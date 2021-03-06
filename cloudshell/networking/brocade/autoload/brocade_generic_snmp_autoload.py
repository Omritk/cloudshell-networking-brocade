import re
import os

import inject
from cloudshell.networking.operations.interfaces.autoload_operations_interface import AutoloadOperationsInterface

from cloudshell.shell.core.driver_context import AutoLoadDetails
from cloudshell.snmp.quali_snmp import QualiMibTable
from cloudshell.networking.autoload.networking_autoload_resource_structure import Port, PortChannel, PowerPort, \
    Chassis, Module
from cloudshell.networking.autoload.networking_autoload_resource_attributes import NetworkingStandardRootAttributes
from cloudshell.networking.brocade.resource_drivers_map import BROCADE_RESOURCE_DRIVERS_MAP


class BrocadeGenericSNMPAutoload(AutoloadOperationsInterface):
    def __init__(self, snmp_handler=None, logger=None, supported_os=None):
        """Basic init with injected snmp handler and logger

        :param snmp_handler:
        :param logger:
        :return:
        """

        self._snmp = snmp_handler
        self._logger = logger
        self.exclusion_list = []
        self._excluded_models = []
        self.module_list = []
        self.chassis_list = []
        self.supported_os = supported_os
        self.port_list = []
        self.power_supply_list = []
        self.relative_path = {}
        self.port_mapping = {}
        self.entity_table_black_list = ['alarm', 'fan', 'sensor', 'other']
        self.port_exclude_pattern = 'serial|stack|engine|management|vlan|other|softwareLoopback|tunnel|fibreChannel|' \
                                    'eth[0-9]'
        self.module_exclude_pattern = 'cevsfp'
        self.resources = list()
        self.attributes = list()

    @property
    def logger(self):
        if self._logger is None:
            try:
                self._logger = inject.instance('logger')
            except:
                raise Exception('BrocadeAutoload', 'Logger is none or empty')
        return self._logger

    @property
    def snmp(self):
        if self._snmp is None:
            try:
                self._snmp = inject.instance('snmp_handler')
            except:
                raise Exception('BrocadeAutoload', 'Snmp handler is none or empty')
        return self._snmp

    def load_brocade_mib(self):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'mibs'))
        self.snmp.update_mib_sources(path)

    def discover(self):
        """Load device structure and attributes: chassis, modules, submodules, ports, port-channels and power supplies

        :return: AutoLoadDetails object
        """

        self._is_valid_device_os()

        self.logger.info('************************************************************************')
        self.logger.info('Start SNMP discovery process .....')

        self.load_brocade_mib()
        self._get_device_details()
        #self.snmp.load_mib(['BROCADE-PRODUCTS-MIB', 'BROCADE-ENTITY-VENDORTYPE-OID-MIB'])
        self._load_snmp_tables()

        if len(self.chassis_list) < 1:
            self.logger.error('Entity table error, no chassis found')
            return AutoLoadDetails(list(), list())

        for chassis in self.chassis_list:
            if chassis not in self.exclusion_list:
                chassis_id = self._get_resource_id(chassis)
                if chassis_id == '-1':
                    chassis_id = '0'
                self.relative_path[chassis] = chassis_id

        self._filter_lower_bay_containers()
        self.get_module_list()
        self.add_relative_paths()
        # Brocade Module start with 1 for index & name. change it to 0
        # for res in self.relative_path:
        #     if len(self.relative_path[res]) == 3:
        #         self.relative_path[res] = self.relative_path[res][:2] + str(int(self.relative_path[res][2])-1)
        #     if len(self.relative_path[res]) > 3:
        #         self.relative_path[res] = self.relative_path[res][:2] + str(int(self.relative_path[res][2]) - 1) + self.relative_path[res][3:]

        self._get_chassis_attributes(self.chassis_list)
        self._get_ports_attributes()
        self._get_module_attributes()
        self._get_power_ports()
        self._get_port_channels()

        result = AutoLoadDetails(resources=self.resources, attributes=self.attributes)

        self.logger.info('*******************************************')
        self.logger.info('Discover completed. The following Structure have been loaded:' +
                         '\nModel, Name, Relative Path, Uniqe Id')

        for resource in self.resources:
            self.logger.info('{0},\t\t{1},\t\t{2},\t\t{3}'.format(resource.model, resource.name,
                                                                  resource.relative_address, resource.unique_identifier))
        self.logger.info('------------------------------')
        for attribute in self.attributes:
            self.logger.info('{0},\t\t{1},\t\t{2}'.format(attribute.relative_address, attribute.attribute_name,
                                                          attribute.attribute_value))

        self.logger.info('*******************************************')
        self.logger.info('SNMP discovery Completed')
        return result

    def _is_valid_device_os(self):
        """Validate device OS using snmp
        :return: True or False
        """

        version = None
        if not self.supported_os:
            config = inject.instance('config')
            self.supported_os = config.SUPPORTED_OS
        system_description = self.snmp.get(('SNMPv2-MIB', 'sysDescr'))['sysDescr']
        match_str = re.sub('[\n\r]+', ' ', system_description.upper())
        res = re.search('\s+(VDX)\s*', match_str)
        if res:
            version = res.group(0).strip(' \s\r\n')
        if version and version in self.supported_os:
            return

        self.logger.info('System description from device: \'{0}\''.format(system_description))

        error_message = 'Incompatible driver! Please use correct resource driver for {0} operation system(s)'. \
            format(str(tuple(self.supported_os)))
        self.logger.error(error_message)
        raise Exception(error_message)

    def _load_snmp_tables(self):
        """ Load all brocade required snmp tables

        :return:
        """

        self.logger.info('Start loading MIB tables:')
        self.if_table = self.snmp.get_table('IF-MIB', 'ifDescr')
        self.logger.info('IfDescr table loaded')
        self.entity_table = self._get_entity_table()
        if len(self.entity_table.keys()) < 1:
            raise Exception('Cannot load entPhysicalTable. Autoload cannot continue')
        self.logger.info('Entity table loaded')

        self.lldp_local_table = self.snmp.get_table('LLDP-MIB', 'lldpLocPortTable')
        self.lldp_remote_table = self.snmp.get_table('LLDP-MIB', 'lldpRemTable')
        self.cdp_index_table = self.snmp.get_table('BROCADE-CDP-MIB', 'cdpInterface')
        self.cdp_table = self.snmp.get_table('BROCADE-CDP-MIB', 'cdpCacheTable')
        self.duplex_table = self.snmp.get_table('EtherLike-MIB', 'dot3StatsIndex')
        self.ip_v4_table = self.snmp.get_table('IP-MIB', 'ipAddrTable')
        self.ip_v6_table = self.snmp.get_table('IPV6-MIB', 'ipv6AddrEntry')
        self.port_channel_ports = self.snmp.get_table('IEEE8023-LAG-MIB', 'dot3adAggPortAttachedAggID')

        self.logger.info('MIB Tables loaded successfully')

    def _get_entity_table(self):
        """Read Entity-MIB and filter out device's structure and all it's elements, like ports, modules, chassis, etc.

        :rtype: QualiMibTable
        :return: structured and filtered EntityPhysical table.
        """

        result_dict = QualiMibTable('entPhysicalTable')

        entity_table_critical_port_attr = {'entPhysicalContainedIn': 'str', 'entPhysicalClass': 'str',
                                           'entPhysicalVendorType': 'str'}
        entity_table_optional_port_attr = {'entPhysicalDescr': 'str', 'entPhysicalName': 'str'}

        physical_indexes = self.snmp.get_table('ENTITY-MIB', 'entPhysicalParentRelPos')
        for index in physical_indexes.keys():
            is_excluded = False
            if physical_indexes[index]['entPhysicalParentRelPos'] == '':
                self.exclusion_list.append(index)
                continue
            temp_entity_table = physical_indexes[index].copy()
            temp_entity_table.update(self.snmp.get_properties('ENTITY-MIB', index, entity_table_critical_port_attr)
                                     [index])
            if temp_entity_table['entPhysicalContainedIn'] == '':
                is_excluded = True
                self.exclusion_list.append(index)

            for item in self.entity_table_black_list:
                if item in temp_entity_table['entPhysicalVendorType'].lower():
                    is_excluded = True
                    break

            if is_excluded is True:
                continue

            temp_entity_table.update(self.snmp.get_properties('ENTITY-MIB', index, entity_table_optional_port_attr)
                                     [index])

            if temp_entity_table['entPhysicalClass'] == '':
                vendor_type = self.snmp.get_property('ENTITY-MIB', 'entPhysicalVendorType', index)
                index_entity_class = None
                if vendor_type == '':
                    continue
                if 'cevcontainer' in vendor_type.lower():
                    index_entity_class = 'container'
                elif 'cevchassis' in vendor_type.lower():
                    index_entity_class = 'chassis'
                elif 'cevmodule' in vendor_type.lower():
                    index_entity_class = 'module'
                elif 'cevport' in vendor_type.lower():
                    index_entity_class = 'port'
                elif 'cevpowersupply' in vendor_type.lower():
                    index_entity_class = 'powerSupply'
                if index_entity_class:
                    temp_entity_table['entPhysicalClass'] = index_entity_class
            else:
                temp_entity_table['entPhysicalClass'] = temp_entity_table['entPhysicalClass'].replace("'", "")

            if re.search('stack|chassis|module|port|powerSupply|container|backplane',
                         temp_entity_table['entPhysicalClass']):
                result_dict[index] = temp_entity_table

            if temp_entity_table['entPhysicalClass'] == 'chassis':
                self.chassis_list.append(index)

            elif temp_entity_table['entPhysicalClass'] == 'powerSupply':
                self.power_supply_list.append(index)
        # Brocade Interfaces sits on the IF-MIB only.
        port_list = self.snmp.get_table('IF-MIB', 'ifTable')
        for index in port_list.keys():
            if not re.search(self.port_exclude_pattern, port_list[index]['ifDescr']) \
                    and not (re.search(self.port_exclude_pattern, port_list[index]['ifType'])):
                self.port_list.append(port_list[index])
        self._filter_entity_table(result_dict)
        return result_dict

    def _filter_lower_bay_containers(self):

        upper_container = None
        lower_container = None
        containers = self.entity_table.filter_by_column('Class', "container").sort_by_column('ParentRelPos').keys()
        for container in containers:
            vendor_type = self.snmp.get_property('ENTITY-MIB', 'entPhysicalVendorType', container)
            if 'uppermodulebay' in vendor_type.lower():
                upper_container = container
            if 'lowermodulebay' in vendor_type.lower():
                lower_container = container
        if lower_container and upper_container:
            child_upper_items_len = len(self.entity_table.filter_by_column('ContainedIn', str(upper_container)
                                                                           ).sort_by_column('ParentRelPos').keys())
            child_lower_items = self.entity_table.filter_by_column('ContainedIn', str(lower_container)
                                                                   ).sort_by_column('ParentRelPos').keys()
            for child in child_lower_items:
                self.entity_table[child]['entPhysicalContainedIn'] = upper_container
                self.entity_table[child]['entPhysicalParentRelPos'] = str(child_upper_items_len + int(
                    self.entity_table[child]['entPhysicalParentRelPos']))

    def add_relative_paths(self):
        """Builds dictionary of relative paths for each module and port

        :return:
        """


        for module in self.module_list:
            if module not in self.exclusion_list:
                self.relative_path[module] = self.get_relative_path(module) + '/' + str(module)
            else:
                self.module_list.remove(module)
        for port in self.port_list:
            port = int(port['suffix'])
            if port not in self.exclusion_list:
                self.relative_path[port] = self.get_relative_path(port) + '/' + str(port)
            else:
                self.port_list.remove(port)

    def _add_resource(self, resource):
        """Add object data to resources and attributes lists

        :param resource: object which contains all required data for certain resource
        """

        self.resources.append(resource.get_autoload_resource_details())
        self.attributes.extend(resource.get_autoload_resource_attributes())

    def get_module_list(self):
        """Set list of all modules from entity mib table for provided list of ports

        :return:
        """
        for entity in self.entity_table:
            if self.entity_table[entity]['entPhysicalClass'] == 'module':
                relpos = int(self.entity_table[entity]['entPhysicalContainedIn'])
                moudle_name = self.entity_table[entity]['entPhysicalName']
                if self.entity_table[relpos]['entPhysicalClass'] == 'chassis':
                    if moudle_name not in self.module_list:
                        self.module_list.append(int(entity))

        # for port in self.full_port_list:
        #     modules = []
        #     modules.append(self._get_module_parents(port))
        #     for module in modules:
        #         if module in self.module_list:
        #             continue
        #         vendor_type = self.snmp.get_property('ENTITY-MIB', 'entPhysicalVendorType', module)
        #         if not re.search(self.module_exclude_pattern, vendor_type.lower()):
        #             if module not in self.exclusion_list and module not in self.module_list:
        #                 self.module_list.append(module)
        #         else:
        #             self._excluded_models.append(module)

    # def _get_module_parents(self, module_id):
    #     result = []
    #     module_id = 'MODULE ' + module_id.split('/')[0][-1:]
    #     for entity in self.entity_table:
    #         if self.entity_table[entity]['entPhysicalName'] == module_id:
    #             parent_id = int(self.entity_table[entity]['entPhysicalContainedIn'])
    #             break
    #
    #     return module_id # parent_id = int(self.entity_table[module_id]['entPhysicalContainedIn'])
    #     if parent_id > 0 and parent_id in self.entity_table:
    #         if re.search('module', self.entity_table[parent_id]['entPhysicalClass']):
    #             result.append(parent_id)
    #             result.extend(self._get_module_parents(parent_id))
    #         elif re.search('chassis', self.entity_table[parent_id]['entPhysicalClass']):
    #             return result
    #         else:
    #             result.extend(self._get_module_parents(parent_id))
    #     return result

    def _get_resource_id(self, item_id):
        if item_id > 500:
            inter = self.if_table[item_id]['ifDescr']
            id = inter.split('/')[1]
            return id
            # parent_id = int(self.if_table[item_id]['ifDescr'].split('/')[0][-1:])
            # mock = 'MODULE ' + str(parent_id)
            # for ent in self.entity_table:
            #     if self.entity_table[ent]['entPhysicalName'] == mock:
            #         if ent in self.module_list:
            #             parent_id = int(self.entity_table[ent]['entPhysicalContainedIn'])
            #             return str(parent_id)
            #             break
        parent_id = int(self.entity_table[item_id]['entPhysicalContainedIn'])
        if parent_id > 0 and parent_id in self.entity_table:
            if re.search('container|backplane', self.entity_table[parent_id]['entPhysicalClass']):
                result = self.entity_table[parent_id]['entPhysicalParentRelPos']
            elif parent_id in self._excluded_models:
                result = self._get_resource_id(parent_id)
            else:
                result = self.entity_table[item_id]['entPhysicalParentRelPos']
        else:
            result = self.entity_table[item_id]['entPhysicalParentRelPos']
        return result

    def _get_chassis_attributes(self, chassis_list):
        """
        Get Chassis element attributes
        :param chassis_list: list of chassis to load attributes for
        :return:
        """

        self.logger.info('Start loading Chassis')
        for chassis in chassis_list:
            chassis_id = self.relative_path[chassis]
            chassis_details_map = {
                'chassis_model': self.snmp.get_property('ENTITY-MIB', 'entPhysicalModelName', chassis),
                'serial_number': self.snmp.get_property('ENTITY-MIB', 'entPhysicalSerialNum', chassis)
            }
            if chassis_details_map['chassis_model'] == '':
                chassis_details_map['chassis_model'] = self.entity_table[chassis]['entPhysicalDescr']
            relative_path = '{0}'.format(chassis_id)
            chassis_object = Chassis(relative_path=relative_path, **chassis_details_map)
            self._add_resource(chassis_object)
            self.logger.info('Added ' + self.entity_table[chassis]['entPhysicalDescr'] + ' Chass')
        self.logger.info('Finished Loading Modules')

    def _get_module_attributes(self):
        """Set attributes for all discovered modules

        :return:
        """

        self.logger.info('Start loading Modules')
        for module in self.module_list:
            module_id = self.relative_path[module]
            module_index = self._get_resource_id(module)
            # Change Brocade Module Name to be -1
            module_index = str(int(module_index) - 1)
            module_details_map = {
                'module_model': self.entity_table[module]['entPhysicalDescr'],
                'version': self.snmp.get_property('ENTITY-MIB', 'entPhysicalSoftwareRev', module),
                'serial_number': self.snmp.get_property('ENTITY-MIB', 'entPhysicalSerialNum', module)
            }

            if '/' in module_id and len(module_id.split('/')) < 3:
                module_name = 'Module {0}'.format(module_index)
                model = 'Generic Module'
            else:
                module_name = 'Sub Module {0}'.format(module_index)
                model = 'Generic Sub Module'
            module_object = Module(name=module_name, model=model, relative_path=module_id, **module_details_map)
            self._add_resource(module_object)

            self.logger.info('Added ' + self.entity_table[module]['entPhysicalDescr'] + ' Module')
        self.logger.info('Finished Loading Modules')

    def _get_power_ports(self):
        """Get attributes for power ports provided in self.power_supply_list

        :return:
        """

        self.logger.info('Start loading Power Ports')
        for port in self.power_supply_list:
            port_id = self.entity_table[port]['entPhysicalParentRelPos']
            parent_index = int(self.entity_table[port]['entPhysicalContainedIn'])
            parent_id = int(self.entity_table[parent_index]['entPhysicalParentRelPos'])
            chassis_id = self.get_relative_path(parent_index)
            relative_path = '{0}/PP{1}-{2}'.format(chassis_id, parent_id, port_id)
            port_name = 'PP{0}'.format(self.power_supply_list.index(port))
            port_details = {'port_model': self.snmp.get_property('ENTITY-MIB', 'entPhysicalModelName', port, ),
                            'description': self.snmp.get_property('ENTITY-MIB', 'entPhysicalDescr', port, 'str'),
                            'version': self.snmp.get_property('ENTITY-MIB', 'entPhysicalHardwareRev', port),
                            'serial_number': self.snmp.get_property('ENTITY-MIB', 'entPhysicalSerialNum', port)
                            }
            power_port_object = PowerPort(name=port_name, relative_path=relative_path, **port_details)
            self._add_resource(power_port_object)

            self.logger.info('Added ' + self.entity_table[port]['entPhysicalName'].strip(' \t\n\r') + ' Power Port')
        self.logger.info('Finished Loading Power Ports')

    def _get_port_channels(self):
        """Get all port channels and set attributes for them

        :return:
        """

        if not self.if_table:
            return
        port_channel_dic = {index: port for index, port in self.if_table.iteritems() if
                            'channel' in port['ifDescr'] and '.' not in port['ifDescr']}
        self.logger.info('Start loading Port Channels')
        for key, value in port_channel_dic.iteritems():
            interface_model = value['ifDescr']
            match_object = re.search('\d+$', interface_model)
            if match_object:
                interface_id = 'PC{0}'.format(match_object.group(0))
            else:
                self.logger.error('Adding of {0} failed. Name is invalid'.format(interface_model))
                continue
            attribute_map = {'description': self.snmp.get_property('IF-MIB', 'ifAlias', key),
                             'associated_ports': self._get_associated_ports(key)}
            attribute_map.update(self._get_ip_interface_details(key))
            port_channel = PortChannel(name=interface_model, relative_path=interface_id, **attribute_map)
            self._add_resource(port_channel)

            self.logger.info('Added ' + interface_model + ' Port Channel')
        self.logger.info('Finished Loading Port Channels')

    def _get_associated_ports(self, item_id):
        """Get all ports associated with provided port channel
        :param item_id:
        :return:
        """

        result = ''
        for key, value in self.port_channel_ports.iteritems():
            if str(item_id) in value['dot3adAggPortAttachedAggID']:
                result += self.if_table[key]['ifDescr'].replace('/', '-').replace(' ', '') + '; '
        return result.strip(' \t\n\r')

    def _get_ports_attributes(self):
        """Get resource details and attributes for every port in self.port_list

        :return:
        """

        self.logger.info('Start loading Ports')
        for port in self.port_list:
            interface_name = port['ifDescr']
            # Add Chassis to Interface name
            chas_id = int(self.get_relative_path(int(port['suffix'])).split('/')[0])
            # Voodoo
            chas_id = str(self.chassis_list[chas_id])
            interface_name = interface_name.split(' ')[0] + ' ' + chas_id + '/' + interface_name.split(' ')[1]
            # Voodoo
            # Replace "/" to "-"
            interface_name = interface_name.replace('/', '-')
            if interface_name == '':
                interface_name = self.entity_table[port]['entPhysicalName']
            if interface_name == '':
                continue
            interface_type = port['ifType'].replace('/', '').replace("'", '').replace('\\', '')
            attribute_map = {'l2_protocol_type': interface_type,
                             'mac': port['ifPhysAddress'],
                             'mtu': port['ifMtu'],
                             'bandwidth': port['ifSpeed'],
                             'description': self.snmp.get('.1.3.6.1.2.1.31.1.1.1.18.' + port['suffix'])['ifAlias'],
                             #'adjacent': self._get_adjacent(self.port_mapping[port])
                             }
            attribute_map.update(self._get_interface_details(port))
            attribute_map.update(self._get_ip_interface_details(port))
            port_object = Port(name=interface_name, relative_path=self.relative_path[int(port['suffix'])],
                               **attribute_map)
            self._add_resource(port_object)
            self.logger.info('Added ' + interface_name + ' Port')
        self.logger.info('Finished Loading Ports')

    def get_relative_path(self, item_id):
        """Build relative path for received item

        :param item_id:
        :return:
        """

        result = ''
        if item_id < 500:
            if item_id not in self.chassis_list:
                parent_id = int(self.entity_table[item_id]['entPhysicalContainedIn'])
                if parent_id not in self.relative_path.keys():
                    if parent_id in self.module_list:
                        result = self._get_resource_id(parent_id)
                    if result != '':
                        result = self.get_relative_path(parent_id) + '/' + result
                    else:
                        result = self.get_relative_path(parent_id)
                else:
                    result = self.relative_path[parent_id]
            else:
                result = self.relative_path[item_id]
        else:
            # its a port

            parent_id = int(self.if_table[item_id]['ifDescr'].split('/')[0][-1:])
            mock = 'MODULE ' + str(parent_id)
            for ent in self.entity_table:
                if self.entity_table[ent]['entPhysicalName'] == mock:
                    if ent in self.module_list:
                        #parent_id = int(self.entity_table[ent]['entPhysicalContainedIn'])
                        parent_id = int(ent)
                        break
            if parent_id not in self.relative_path.keys():
                if parent_id in self.module_list:
                    result = self._get_resource_id(parent_id)
                if result != '':
                    result = self.get_relative_path(parent_id) + '/' + result
                else:
                    result = self.get_relative_path(parent_id)
            else:
                result = self.relative_path[parent_id]

        return result

    def _filter_entity_table(self, raw_entity_table):
        """Filters out all elements if their parents, doesn't exist, or listed in self.exclusion_list

        :param raw_entity_table: entity table with unfiltered elements
        """

        elements = raw_entity_table.filter_by_column('ContainedIn').sort_by_column('ParentRelPos').keys()
        for element in reversed(elements):
            parent_id = int(self.entity_table[element]['entPhysicalContainedIn'])

            if parent_id not in raw_entity_table or parent_id in self.exclusion_list:
                self.exclusion_list.append(element)

    def _get_ip_interface_details(self, port_index):
        """Get IP address details for provided port

        :param port_index: port index in ifTable
        :return interface_details: detected info for provided interface dict{'IPv4 Address': '', 'IPv6 Address': ''}
        """

        interface_details = {'ipv4_address': '', 'ipv6_address': ''}
        if self.ip_v4_table and len(self.ip_v4_table) > 1:
            for key, value in self.ip_v4_table.iteritems():
                if 'ipAdEntIfIndex' in value and int(value['ipAdEntIfIndex']) == port_index:
                    interface_details['IPv4 Address'] = key
                break
        if self.ip_v6_table and len(self.ip_v6_table) > 1:
            for key, value in self.ip_v6_table.iteritems():
                if 'ipAdEntIfIndex' in value and int(value['ipAdEntIfIndex']) == port_index:
                    interface_details['IPv6 Address'] = key
                break
        return interface_details

    def _get_interface_details(self, port_index):
        """Get interface attributes

        :param port_index: port index in ifTable
        :return interface_details: detected info for provided interface dict{'Auto Negotiation': '', 'Duplex': ''}
        """

        interface_details = {'duplex': 'Full', 'auto_negotiation': 'False'}
        try:
            auto_negotiation = self.snmp.get(('MAU-MIB', 'ifMauAutoNegAdminStatus', port_index, 1)).values()[0]
            if 'enabled' in auto_negotiation.lower():
                interface_details['auto_negotiation'] = 'True'
        except Exception as e:
            self.logger.error('Failed to load auto negotiation property for interface {0}'.format(e.message))
        for key, value in self.duplex_table.iteritems():
            if 'dot3StatsIndex' in value.keys() and value['dot3StatsIndex'] == str(port_index):
                interface_duplex = self.snmp.get_property('EtherLike-MIB', 'dot3StatsDuplexStatus', key)
                if 'halfDuplex' in interface_duplex:
                    interface_details['duplex'] = 'Half'
        return interface_details

    def _get_device_details(self):
        """Get root element attributes

        """

        self.logger.info('Start loading Switch Attributes')
        result = {'system_name': self.snmp.get_property('SNMPv2-MIB', 'sysName', 0),
                  'vendor': 'Brocade',
                  'model': self._get_device_model(),
                  'location': self.snmp.get_property('SNMPv2-MIB', 'sysLocation', 0),
                  'contact': self.snmp.get_property('SNMPv2-MIB', 'sysContact', 0),
                  # Get Brocade FW OS Version directly
                  'version': self.snmp.get('.1.3.6.1.4.1.1588.2.1.1.1.1.6.0')['enterprises']}

        match_version = re.search('Version\s+(?P<software_version>\S+)\S*\s+',
                                  self.snmp.get_property('SNMPv2-MIB', 'sysDescr', 0))
        if match_version:
            result['version'] = match_version.groupdict()['software_version'].replace(',', '')

        root = NetworkingStandardRootAttributes(**result)
        self.attributes.extend(root.get_autoload_resource_attributes())
        self.logger.info('Finished Loading Switch Attributes')

    def _get_adjacent(self, interface_id):
        """Get connected device interface and device name to the specified port id, using cdp or lldp protocols

        :param interface_id: port id
        :return: device's name and port connected to port id
        :rtype string
        """

        result = ''
        for key, value in self.cdp_table.iteritems():
            if 'cdpCacheDeviceId' in value and 'cdpCacheDevicePort' in value:
                if re.search('^\d+', str(key)).group(0) == interface_id:
                    result = '{0} through {1}'.format(value['cdpCacheDeviceId'], value['cdpCacheDevicePort'])
        if result == '' and self.lldp_remote_table:
            for key, value in self.lldp_local_table.iteritems():
                interface_name = self.if_table[interface_id]['ifDescr']
                if interface_name == '':
                    break
                if 'lldpLocPortDesc' in value and interface_name in value['lldpLocPortDesc']:
                    if 'lldpRemSysName' in self.lldp_remote_table and 'lldpRemPortDesc' in self.lldp_remote_table:
                        result = '{0} through {1}'.format(self.lldp_remote_table[key]['lldpRemSysName'],
                                                          self.lldp_remote_table[key]['lldpRemPortDesc'])
        return result

    def _get_device_model(self):
        """Get device model form snmp SNMPv2 mib

        :return: device model
        :rtype: str
        """

        result = ''
        snmp_object_id = self.snmp.get_property('SNMPv2-MIB', 'sysObjectID', 0)
        match_name = re.search(r'\.(?P<model>\d+$)', snmp_object_id)
        if match_name:
            model = match_name.groupdict()['model']
            if model in BROCADE_RESOURCE_DRIVERS_MAP:
                result = BROCADE_RESOURCE_DRIVERS_MAP[model].lower().replace('_', '').capitalize()
        if not result or result == '':
            self.snmp.load_mib(['BROCADE-PRODUCTS-MIB', 'BROCADE-ENTITY-VENDORTYPE-OID-MIB'])
            match_name = re.search(r'::(?P<model>\S+$)', self.snmp.get_property('SNMPv2-MIB', 'sysObjectID', '0'))
            if match_name:
                result = match_name.groupdict()['model'].capitalize()
        return result

    def _get_mapping(self, port_index, port_descr):
        """ Get mapping from entPhysicalTable to ifTable.
        Build mapping based on ent_alias_mapping_table if exists else build manually based on
        entPhysicalDescr <-> ifDescr mapping.

        :return: simple mapping from entPhysicalTable index to ifTable index:
        |        {entPhysicalTable index: ifTable index, ...}
        """

        port_id = None
        try:
            ent_alias_mapping_identifier = self.snmp.get(('IF-MIB', 'entAliasMappingIdentifier', port_index, 0))
            port_id = int(ent_alias_mapping_identifier['entAliasMappingIdentifier'].split('.')[-1])
        except Exception as e:
            self.logger.error(e.message)
            module_index, port_index = re.findall('\d+', port_descr)
            if_table_re = '^.*' + module_index + '/' + port_index + '$'
            for interface in self.if_table.values():
                if re.search(if_table_re, interface['ifDescr']):
                    port_id = int(interface['suffix'])
                    break
        return port_id
