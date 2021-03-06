#
# Copyright (c) 2016 Juniper Networks, Inc. All rights reserved.
#

"""
VNC service management for kubernetes
"""

from vnc_api.vnc_api import *
from config_db import *
from loadbalancer import *
from cfgm_common import importutils
import link_local_manager as ll_mgr

class VncService(object):

    def __init__(self, vnc_lib=None, label_cache=None, args=None, logger=None,
                 kube=None):
        self._vnc_lib = vnc_lib
        self._label_cache = label_cache
        self._args = args
        self.logger = logger
        self.kube = kube

        self._fip_pool_obj = None

        # Cache kubernetes API server params.
        self._kubernetes_api_secure_ip = args.kubernetes_api_secure_ip
        self._kubernetes_api_secure_port = int(args.kubernetes_api_secure_port)

        # Cache kuberneter service name.
        self._kubernetes_service_name = args.kubernetes_service_name

        # Config knob to control enable/disable of link local service.
        if args.api_service_link_local == 'True':
            api_service_ll_enable = True
        else:
            api_service_ll_enable = False

        # If Kubernetes API server info is incomplete, disable link-local create,
        # as create is not possible.
        if not self._kubernetes_api_secure_ip or\
            not self._kubernetes_api_secure_ip:
            self._create_linklocal = False
        else:
            self._create_linklocal = api_service_ll_enable
                                        
        self.service_lb_mgr = importutils.import_object(
            'kube_manager.vnc.loadbalancer.ServiceLbManager', vnc_lib, logger)
        self.service_ll_mgr = importutils.import_object(
            'kube_manager.vnc.loadbalancer.ServiceLbListenerManager', vnc_lib, logger)
        self.service_lb_pool_mgr = importutils.import_object(
            'kube_manager.vnc.loadbalancer.ServiceLbPoolManager', vnc_lib, logger)
        self.service_lb_member_mgr = importutils.import_object(
            'kube_manager.vnc.loadbalancer.ServiceLbMemberManager', vnc_lib, logger)

    def _get_project(self, service_namespace):
        proj_fq_name = ['default-domain', service_namespace]
        try:
            proj_obj = self._vnc_lib.project_read(fq_name=proj_fq_name)
            return proj_obj
        except NoIdError:
            return None

    def _get_network(self):
        vn_fq_name = ['default-domain', 'default', 'cluster-network']
        try:
            vn_obj = self._vnc_lib.virtual_network_read(fq_name=vn_fq_name)
        except NoIdError:
            return None
        return vn_obj

    def _get_public_fip_pool(self):
        if self._fip_pool_obj:
            return self._fip_pool_obj
        fip_pool_fq_name = ['default-domain', 'default', 
                            self._args.public_network_name, 
                            self._args.public_fip_pool_name]
        try:
            fip_pool_obj = self._vnc_lib.floating_ip_pool_read(fq_name=fip_pool_fq_name)
        except NoIdError:
            return None

        self._fip_pool_obj = fip_pool_obj
        return fip_pool_obj

    def _get_virtualmachine(self, id):
        try:
            vm_obj = self._vnc_lib.virtual_machine_read(id=id)
        except NoIdError:
            return None
        obj = self._vnc_lib.virtual_machine_read(id = id, fields = ['virtual_machine_interface_back_refs'])
        back_refs = getattr(obj, 'virtual_machine_interface_back_refs', None)
        vm_obj.virtual_machine_interface_back_refs = back_refs
        return vm_obj

    def check_service_selectors_actions(self, selectors, service_id, ports):
        for selector in selectors.items():
            key = self._label_cache._get_key(selector)
            self._label_cache._locate_label(key,
                self._label_cache.service_selector_cache, selector, service_id)
            pod_ids = self._label_cache.pod_label_cache.get(key, [])
            if len(pod_ids):
                self.add_pods_to_service(service_id, pod_ids, ports)

    def _vnc_create_pool(self, namespace, ll, port):
        proj_obj = self._get_project(namespace)
        ll_obj = self.service_ll_mgr.read(ll.uuid)
        pool_obj = self.service_lb_pool_mgr.create(ll_obj, proj_obj, port)
        return pool_obj

    def _vnc_create_listener(self, namespace, lb, port):
        proj_obj = self._get_project(namespace)
        lb_obj = self.service_lb_mgr.read(lb.uuid)
        ll_obj = self.service_ll_mgr.create(lb_obj, proj_obj, port)
        return ll_obj

    def _create_listeners(self, namespace, lb, ports):
        for port in ports:
            listener_found = False
            for ll_id in lb.loadbalancer_listeners:
                ll = LoadbalancerListenerKM.get(ll_id)
                if not ll:
                    continue
                if not ll.params['protocol_port']:
                    continue
                if not ll.params['protocol']:
                    continue

                if ll.params['protocol_port'] == port['port'] and \
                   ll.params['protocol'] == port['protocol']:
                    listener_found = True
                    break

            if not listener_found:
                ll_obj = self._vnc_create_listener(namespace, lb, port)
                ll = LoadbalancerListenerKM.locate(ll_obj._uuid)

            pool_id = ll.loadbalancer_pool
            if pool_id:
                pool = LoadbalancerPoolKM.get(pool_id)
            # SAS FIXME: If pool_id present, check for targetPort value
            if not pool_id or not pool:
                pool_obj = self._vnc_create_pool(namespace, ll, port)
                LoadbalancerPoolKM.locate(pool_obj._uuid)

    def _create_link_local_service(self, svc_name, svc_ip, ports):
        # Create link local service only if enabled.
        if self._create_linklocal:
            # Create link local service, one for each port.
            for port in ports:
                try:
                    ll_mgr.create_link_local_service_entry(self._vnc_lib,
                        name=svc_name + '-' + port['port'].__str__(),
                        service_ip=svc_ip, service_port=port['port'],
                        fabric_ip=self._kubernetes_api_secure_ip,
                        fabric_port=self._kubernetes_api_secure_port)
                except:
                    self.logger.error("Create link-local service failed for"
                        " service " + svc_name + " port " +
                        port['port'].__str__())

    def _delete_link_local_service(self, svc_name, svc_ip, ports):
        # Delete link local service only if enabled.
        if self._create_linklocal:
            # Delete link local service, one for each port.
            for port in ports:
                try:
                    ll_mgr.delete_link_local_service_entry(vnc_lib,
                        name=svc_name + '-' + port['port'].__str__())
                except:
                    self.logger.error("Delete link local service failed for"
                        " service " + svc_name + " port " +
                        port['port'].__str__())

    def _vnc_create_lb(self, service_id, service_name,
                       service_namespace, service_ip):
        proj_obj = self._get_project(service_namespace)
        vn_obj = self._get_network()
        lb_provider = 'native'
        lb_obj = self.service_lb_mgr.create(lb_provider, vn_obj, service_id,
                                        service_name, proj_obj, service_ip)
        return lb_obj

    def _lb_create(self, service_id, service_name,
            service_namespace, service_ip, ports):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            lb_obj = self._vnc_create_lb( service_id, service_name,
                                        service_namespace, service_ip)
            lb = LoadbalancerKM.locate(service_id)

        self._create_listeners(service_namespace, lb, ports)

    def _get_floating_ip(self, service_id):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            return
        vmi_ids = lb.virtual_machine_interfaces
        if vmi_ids is None:
            return None
        interface_found=False
        for vmi_id in vmi_ids:
            vmi = VirtualMachineInterfaceKM.get(vmi_id)
            if vmi is not None:
                interface_found=True
                break
        if interface_found is False:
            return

        fip_ids = vmi.floating_ips
        if fip_ids is None:
            return None
        for fip_id in list(fip_ids):
            fip = FloatingIpKM.get(fip_id)
            if fip is not None:
                return fip.address

        return None

    def _allocate_floating_ip(self, service_id, external_ip=None):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            return None
        vmi_ids = lb.virtual_machine_interfaces
        if vmi_ids is None:
            return None
        interface_found=False
        for vmi_id in vmi_ids:
            vmi = VirtualMachineInterfaceKM.get(vmi_id)
            if vmi is not None:
                interface_found=True
                break
        if interface_found is False:
            return

        vmi_obj = self._vnc_lib.virtual_machine_interface_read(id=vmi_id)
        if vmi_obj is None:
            return None

        fip_pool = self._get_public_fip_pool()
        if fip_pool is None:
            self.logger.warning("public_fip_pool [%s, %s] doesn't exists" %
                                 (self.config.public_network_name,
                                 self.config.public_fip_pool_name))
            return None

        fip_obj = FloatingIp(lb.name + "-externalIP", fip_pool)
        fip_obj.set_virtual_machine_interface(vmi_obj)
        if external_ip:
            fip_obj.set_floating_ip_address(external_ip)
        project = self._vnc_lib.project_read(id=lb.parent_uuid)
        fip_obj.set_project(project)
        try:
            self._vnc_lib.floating_ip_create(fip_obj)
        except RefsExistError as e:
            err_msg = cfgm_common.utils.detailed_traceback()
            self.logger.error(err_msg)
            return None
        except:
            err_msg = cfgm_common.utils.detailed_traceback()
            self.logger.error(err_msg)
            return None
        fip = FloatingIpKM.locate(fip_obj.uuid)
        return fip.address

    def _deallocate_floating_ip(self, service_id):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            return
        vmi_ids = lb.virtual_machine_interfaces
        if vmi_ids is None:
            return None
        interface_found=False
        for vmi_id in vmi_ids:
            vmi = VirtualMachineInterfaceKM.get(vmi_id)
            if vmi is not None:
                interface_found=True
                break
        if interface_found is False:
            return

        fip_ids = vmi.floating_ips.copy()
        for fip_id in fip_ids:
            self._vnc_lib.floating_ip_delete(id=fip_id)

    def _update_service_external_ip(self, service_namespace, service_name, external_ip):
        merge_patch = {'spec': {'externalIPs': [external_ip]}}
        self.kube.patch_resource(resource_type="services", resource_name=service_name,
                           namespace=service_namespace, merge_patch=merge_patch)

    def _update_service_public_ip(self, service_id, service_name,
                        service_namespace, service_type, external_ip, loadBalancerIp):
        allocated_fip = self._get_floating_ip(service_id)
        if service_type in ["LoadBalancer"]:
            if allocated_fip is None and loadBalancerIp is not None:
                allocated_fip = self._allocate_floating_ip(service_id, loadBalancerIp)
                if external_ip != allocated_fip:
                    self._update_service_external_ip(service_namespace, service_name, allocated_fip)
                return

            if allocated_fip is None and loadBalancerIp is None:
                allocated_fip = self._allocate_floating_ip(service_id)
                if external_ip != allocated_fip:
                    self._update_service_external_ip(service_namespace, service_name, allocated_fip)
                return

            if allocated_fip is not None and loadBalancerIp is None:
                if external_ip != allocated_fip:
                    self._update_service_external_ip(service_namespace, service_name, allocated_fip)
                return

            if allocated_fip and loadBalancerIp and allocated_fip == loadBalancerIp:
                if external_ip is None:
                    self._update_service_external_ip(service_namespace, service_name, allocated_fip)
                return

        if service_type in ["ClusterIP"]:
            if allocated_fip is not None and external_ip is None:
                self._deallocate_floating_ip(service_id)
                return

            if allocated_fip is None and external_ip is None:
                return

            if allocated_fip is not None and external_ip is not None:
                if external_ip != allocated_fip:
                    self._deallocate_floating_ip(service_id)
                    self._allocate_floating_ip(service_id, external_ip)
                    self._update_service_external_ip(service_namespace, service_name, external_ip)
                return

            if allocated_fip is None and external_ip is not None:
                self._allocate_floating_ip(service_id, external_ip)
                return

    def _check_service_uuid_change(self, svc_uuid, svc_name, 
                                   svc_namespace, ports):
        lb_fq_name = ['default-domain', svc_namespace, svc_name]
        lb_uuid = LoadbalancerKM.get_fq_name_to_uuid(lb_fq_name)
        if lb_uuid != svc_uuid:
            self.vnc_service_delete(lb_uuid, svc_name, svc_namespace, ports)

    def vnc_service_add(self, service_id, service_name,
                        service_namespace, service_ip, selectors, ports,
                        service_type, externalIp, loadBalancerIp):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            self._check_service_uuid_change(service_id, service_name, 
                                            service_namespace, ports)

        self._lb_create(service_id, service_name, service_namespace,
                        service_ip, ports)

        # "kubernetes" service needs a link-local service to be created.
        # This link-local service will steer traffic destined for
        # "kubernetes" service from slave (compute) nodes to kube-api server 
        # running on master (control) node.
        if service_name == self._kubernetes_service_name:
            self._create_link_local_service(service_name, service_ip, ports)

        self._update_service_public_ip(service_id, service_name,
                        service_namespace, service_type, externalIp, loadBalancerIp)


    def _vnc_delete_pool(self, pool_id):
        self.service_lb_pool_mgr.delete(pool_id)

    def _vnc_delete_listener(self, ll_id):
        self.service_ll_mgr.delete(ll_id)

    def _vnc_delete_listeners(self, lb):
        listeners = lb.loadbalancer_listeners.copy()
        for ll_id in listeners or []:
            ll = LoadbalancerListenerKM.get(ll_id)
            if not ll:
                continue
            pool_id = ll.loadbalancer_pool
            if pool_id:
                pool = LoadbalancerPoolKM.get(pool_id)
                if pool:
                    members = pool.members.copy()
                    for member_id in members or []:
                        member = LoadbalancerMemberKM.get(member_id)
                        if member:
                            self.service_lb_member_mgr.delete(member_id)
                            LoadbalancerMemberKM.delete(member_id)

                self._vnc_delete_pool(pool_id)
                LoadbalancerPoolKM.delete(pool_id)
            self._vnc_delete_listener(ll_id)
            LoadbalancerListenerKM.delete(ll_id)

    def _vnc_delete_lb(self, lb_id):
        self.service_lb_mgr.delete(lb_id)

    def _lb_delete(self, service_id, service_name,
                   service_namespace):
        lb = LoadbalancerKM.get(service_id)
        if not lb:
            return
        self._vnc_delete_listeners(lb)
        self._vnc_delete_lb(service_id)
        LoadbalancerKM.delete(service_id)

    def vnc_service_delete(self, service_id, service_name,
                           service_namespace, ports):
        self._deallocate_floating_ip(service_id)
        self._lb_delete(service_id, service_name, service_namespace)

        # Delete link local service that would have been allocated for
        # kubernetes service.
        if service_name == self._kubernetes_service_name:
            _delete_link_local_service(service_name, svc_ip, ports)

    def process(self, event):
        service_id = event['object']['metadata'].get('uid')
        service_name = event['object']['metadata'].get('name')
        service_namespace = event['object']['metadata'].get('namespace')
        service_ip = event['object']['spec'].get('clusterIP')
        selectors = event['object']['spec'].get('selector', None)
        ports = event['object']['spec'].get('ports')
        service_type  = event['object']['spec'].get('type')
        loadBalancerIp  = event['object']['spec'].get('loadBalancerIP', None)
        externalIps  = event['object']['spec'].get('externalIPs', None)
        if externalIps is not None:
            externalIp = externalIps[0]
        else:
            externalIp = None

        if event['type'] == 'ADDED' or event['type'] == 'MODIFIED':
            self.vnc_service_add(service_id, service_name,
                service_namespace, service_ip, selectors, ports,
                service_type, externalIp, loadBalancerIp)
        elif event['type'] == 'DELETED':
            self.vnc_service_delete(service_id, service_name, service_namespace,
                                    ports)
