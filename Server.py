#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
该类只负责创建一个Server连接，消息处理交给Client类
"""
from random import Random
import networkx as nx
from gevent import monkey;monkey.patch_all()
import gevent
import socket
import time
import contextlib
import json
# from sqlalchemy.orm import sessionmaker
# from db.connect import ConnectDB
# from db.models import Controller,SwitchesLoad
from Client import Client
from gevent import spawn
from gevent import subprocess
from threading import Thread
from StreamInfo import InfoProcess
import settings
import numpy as np
from typing import List,Dict
import utils
from vikor import Vikor
from copy import deepcopy
from db.core import *
"""
服务初始信息
"""
IP=settings.IP     #IP

PORT=settings.PORT          #端口号

WAIT_CONNECT=settings.WAIT_CONNECT  #最大等待连接数

SAVE_DATA_PERIOD=10 #保存到数据库的周期间隔

CONTROLLER_LOAD_THRESHOLD=settings.CONTROLLER_PKT_THRESHOLD #控制器负载阈值

EDGE_LINK=settings.EDGE_LINK#边界链路

ADJACENCY_CONTROLLER=settings.ADJACENCY_CONTROLLER#最近域控制器

SW_LINK=settings.SW_LINK #链路

CONTROLLER_EDGE_SWITCHES=settings.CONTROLLER_EDGE_SWITCHES #控制器的边缘交换机

CONTROLLERS_EDGE_SWITCHES_AREA=settings.CONTROLLERS_EDGE_SWITCHES_AREA
"""
    controller_id 每一个控制器的IP从0开始，具体从几开始，要看网络的子网号，具体请看mininet拓扑文件中的配置信息
"""


"""
交换机迁移备选方案格式：
plan={

}
"""
random=Random()
class Server(object):
    def __init__(self, *args,**kwargs):
        #初始化服务类
        super(Server, self).__init__()

        self.controller_id = 0 #控制器ID从1开始

        self.controller_obj={}#

        self.server = self.start_server()

        self.log=InfoProcess()

        #self.db=ConnectDB()

        #全局信息类
        self.switches={}#交换机从属表 {dpid:master_controller|area_id}#迁移更改项

        self.controller_to_switches={}#控制器控制的交换机表 {c1:[s11,s12]....} #迁移更改项

        self.topo={}#拓扑信息 {(dpid,dpid):(port,port)} 永远不变

        self.paths={}#全域路径表 {(ip_src,ip_dst):[path]}  只有最短路径

        self.dpaths={}#全域路径表{(src_dpid,dst_dpid):[path]} 只有最短路径

        self.sw_ip={}#全局IP-主机-Area映射表 {(sw,port):{"ip":xxxx,"area_id":xx}} 迁移更改项

        self.edge_sw_area=CONTROLLERS_EDGE_SWITCHES_AREA#边界交换机 {(dpid,port):area_id} 边界交换机端口所对应的area,迁移更改项

        self.edge_sw=None

        self.adjacency_controller=None

        self.arp_table={}#全局mac地址对应 {ip:mac}

        self.FLOOD_IP=[]#全局FLOOD代理表，{controller_id:[ip1....ipn]}

        #全局负载类

        self.controller_pktin_load={}    #{c1:{pktin:xxx,delay:xxx}}

        self.switches_pktin_load={}  #{c1:{sw1:{pktin_speed:xxx,percentage：xxx,pktin_size:xxx}....}} 迁移修改项

        #拓扑计算类
        self.graph=nx.DiGraph()

        #拓扑初始化类
        self.init_edge_link()

        self.init_edge_sw()

        self.init_adjacency_controller()
        #策略执行
        self.strategy="lsm"

    #========初始化TOPO========
    """
        由于不同控制area的边界控制器判断逻辑失误，所以直接送setting中的EDGE_LINK进行初始化
    """
    def init_edge_sw(self):
        #初始化边界交换机
        edge_sw={}

        for c in map(utils.strip_c,CONTROLLER_EDGE_SWITCHES.keys()):
            switches=[]
            for j in map(utils.strip_s,CONTROLLER_EDGE_SWITCHES[f'c{c}']):
                switches.append(j)
            edge_sw[c]=switches
        self.edge_sw=edge_sw

    def init_adjacency_controller(self):
        #初始化领接控制器
        adjacency_controller = {}
        for c in map(utils.strip_c, ADJACENCY_CONTROLLER.keys()):
            controllers = []
            for j in map(utils.strip_c, ADJACENCY_CONTROLLER[f'c{c}']):
                controllers.append(j)
            adjacency_controller[c] = controllers

        self.adjacency_controller=adjacency_controller

    def init_edge_link(self):
        for sw,port in EDGE_LINK.items():
            src_dpid, dst_dpid, src_port, dst_port = int(sw[0].split('s')[1]), int(sw[1].split('s')[1]), port[0], port[1]
            self.graph.add_edge(src_dpid, dst_dpid,src_port=src_port,dst_port=dst_port)
            self.graph.add_edge(dst_dpid, src_dpid, src_port=dst_port, dst_port=src_port)
            self.topo[(src_dpid, dst_dpid)] = (src_port, dst_port)
    #========初始化服务器========
    def start_server(self):
        server=socket.socket()
        server.bind((IP,PORT))
        server.listen(WAIT_CONNECT)
        return server

    def accept_client(self):
        while True:
            controller, addr = self.server.accept()  # 为每一个client保存为一个对象，返回每一个操控该客户端的socket句柄，也就是client
            self.controller_id += 1
            p= Thread(target=self.start_client, args=(controller, addr))#只能开线程，进程隔离
            p.start()#开启进程

    def start_client(self,controller,addr):
        self.log.warning(f'{addr} has connected!!')
        with contextlib.closing(Client(controller)) as c:
            c.status=True#状态设为已连接
            c.server=self
            c.cur_id=self.controller_id
            self.controller_obj[self.controller_id-1] = c#存储controller的消息处理,key值从0开始，即0对应的控制器对象为控制器c1
            c.set_controller_id()
            c.start_spawn()

    def remove_client(self,controller_id):
        """
        :param controller_id: 控制器ID
        :return: 控制器下线
        """
        controller=self.controller_obj[controller_id]
        if controller:
            controller.close()
            self.controller_obj.pop(controller_id)
            print(f'控制器：{controller_id}  下线！')

    def start(self):
        print("Server start...")
        time.sleep(5)
        spawn(self.monitor)  # 协程监控，打印全局拓扑

    def get_statistic_load_rate(self):
        #获取控制器之间的负载均衡度
        rate=0
        controller_load=self.controller_pktin_load
        if not controller_load:
            return 0
        num=len(controller_load.keys())

        pktin_load_avg=self.get_avg_load()

        for load in controller_load.values():
            r=(load.get('pktin')-pktin_load_avg)**2
            rate+=r

        return round(rate/num,4)

    def get_avg_load(self):
        # 获取集群的平均负载
        pktin_load = 0
        controller_load = self.controller_pktin_load
        if not controller_load:
            return 0
        num = len(controller_load.keys())

        for load in controller_load.values():
            pktin_load += load["pktin"]

        pktin_load_avg = round(pktin_load / num, 4)

        return pktin_load_avg

    def get_adjacency_switches(self,switch)->List:

        return list(self.graph.neighbors(switch))

    def exist_edge(self,u,v)->bool:
        return self.graph.has_edge(u,v)

    def get_controller_load(self,controller):
        return self.controller_pktin_load[controller]["pktin"]

    def is_controller_overload(self,controller):
        #判断控制器是否过载,返回控制器编号
        return controller if self.controller_pktin_load[controller]["pktin"] >= CONTROLLER_LOAD_THRESHOLD else False

    def balance_check(self):
        #检查集群状态信息
        #判断是否需要进行交换机迁移,采取最近域迁移原则
        def exclude_False(x):
            if x:
                return x
        controllers=list(self.controller_pktin_load.keys())#[c1,c2,c3,c4,c5]
        result=[]
        for c in controllers:
            result.append(self.is_controller_overload(c))
        if any(result):#需要进行交换机迁移
            overload_controllers=list(filter(exclude_False,result))#过载控制器
            for c in overload_controllers:
                
                spawn(self.start_controller_switches_migration,c) #对每个决策开启协程处理
        else:#不需要进行交换机迁移
            self.log.info("集群稳定")

    def start_controller_switches_migration(self,controller):
        #分析每个控制器的迁移方案，根据迁移方案的不同，执行不同的策略
        self.log.warning(f"控制器{controller}过载进行交换机迁移,执行策略：{self.strategy}")
        if self.strategy.__eq__('vikor'):#vikor迁移
            spawn(self.vikor_strategy,controller)
        if self.strategy.__eq__('static'):
            return
        if self.strategy.__eq__('csm'):#随机迁移
            spawn(self.csm_strategy,controller)
        if self.strategy.__eq__('lsm'):#最轻迁移
            spawn(self.lsm_strategy,controller)
            
    def search_migration_plan(self,**kwargs):
        """
        核心逻辑
        向内扩散，选取交换机集合，返回如下格式的迁移方案
        migration_plan={controller:[[s1,s3....],[s3,s4...]]}
        """
        migration_plan={}
        balance_load=kwargs["balance_load"]#需要达到的平衡阈值，选取的迁移交换机集合的负载大小大于该值即可
        switches=kwargs["switches"]#过载控制器掌管的交换机
        switches_pkt_load=kwargs["switches_pkt_load"]#过载控制器下的交换机负载信息
        dest_controller=kwargs["dest_controller"]
        for controller in dest_controller:
            migration_plan.setdefault(controller,[])
        
        # def filter_sw(sw):
        #     if sw in self.controller_to_switches[kwargs["controller"]]:
        #         return sw
        # for switch in switches:#O(n3)复杂度
        #     #处理的主逻辑
        #     ready_switches = [switch] # 待迁移的交换机集合,先将首个交换机插入，然后依次判定
        #     ready_switches_load =switches_pkt_load[switch].get('pktin_speed')
        #     adjacency_nodes=list(filter(filter_sw,self.get_adjacency_switches(switch)))#获取节点的相邻节点，
        #     # 并排除非本地域的交换机
        #     if ready_switches_load>=balance_load:#如果当前边缘交换机恰好满足负载阈值需求，则直接放入迁移集合
        #         for dest in dest_controller:#选择目的控制器
        #             after_migration_controller_load = ready_switches_load + self.get_controller_load(dest)
        #             if after_migration_controller_load <= CONTROLLER_LOAD_THRESHOLD:
        #                 # 如果迁移过后不过载，加入到迁移方案集合
        #                 migration_plan[dest].append(ready_switches)
        #             else:
        #                 continue
        #
        #     else:
        #         for adjacency_sw in adjacency_nodes:
        #             sum_load=ready_switches_load+switches_pkt_load[adjacency_sw].get('pktin_speed')
        #             if sum_load<balance_load:
        #                 #不满足则继续向内扩,只扩到边界交换机的领接节点
        #                 ready_switches.append(adjacency_sw)
        #                 ready_switches_load=sum_load
        #                 continue
        #             else:
        #                 #满足的负载阈值，则进行平衡负载，判断应该加入到哪个控制器，并保证加入后迁入控制器不过载,并不一定要保证sumload大于平衡阈值，退而求其次
        #                 #load=sum([switches_pkt_load[sw].get('pktin_speed') for sw in ready_switches])#计算迁移方案的负载综合
        #                 for dest in dest_controller:
        #                     after_migration_controller_load=sum_load+self.get_controller_load(dest)
        #                     if after_migration_controller_load<=CONTROLLER_LOAD_THRESHOLD:
        #                         ready_switches.append(adjacency_sw)  # 将这个交换机加入到待迁移的集合中
        #                         #如果迁移过后不过载，加入到迁移方案集合
        #                         migration_plan[dest].append(ready_switches)
        #                     else:
        #                         #退而求其次，表明加入了该交换机虽然能够使其超过平均阈值，但是迁入的控制器会过载，则不将该交换机迁入
        #                         migration_plan[dest].append(ready_switches)
        sorted_sw_load = { k : v for k, v in
                           sorted(switches_pkt_load.items(), key = lambda item : item[1]['pktin_speed'], reverse = True) }

        # 创建新的字典，格式为{（order，sw）: pkt的值}
        sw_load = { i + 1 : (k, v['pktin_speed']) for i, (k, v) in enumerate(sorted_sw_load.items()) }
        
        # 转化为列表
        load_list = [sw_load[i + 1][1] for i, k in enumerate(sorted_sw_load.keys())]
    
        sw_load_list,sw_index_list=utils.generate_combinations(arr=load_list,sw_load = sw_load)
        
        for dst in dest_controller :
            for ml,ms in zip(sw_load_list,sw_index_list):
                plan_sum_load=np.sum(ml)
                if plan_sum_load>=balance_load:
                    migration_plan[dst].append(ms)

        return migration_plan
    
    def estimate_cost(self,plan,**kwargs):
        #评估迁移方案所带来的代价,负载均衡度与平均迁移代价
        #plan={dst_controller:[[s1,s2....],[s2,s3....]]}
        controller=kwargs["controller"]
        switches_pktin_load=self.switches_pktin_load[controller]
        cost_plan={}#{plan_order:{dest_controller:dest_controller
        #                         migration_set:[sw..]
        #                         variance:...}}
        plan_order=0
        #将方案编号,从0开始
        #评价每个迁移方案过后的集群负载均衡度
        pre_load=self.controller_pktin_load.get(controller).get('pktin')
        for dest_controller,switches_migration_set_list in plan.items():
            #=======计算负载均衡度指标，成本指标
            for switches_migration_set in switches_migration_set_list:
                controller_load = deepcopy(self.controller_pktin_load)#必须深拷贝，不然会影响self的值

                cost=sum([switches_pktin_load.get(sw).get("pktin_speed")
                          for sw in switches_migration_set])#该方案的负载变化

                controller_load[controller]['pktin']=pre_load-cost#预估迁移走后控制器的负载是多少
                controller_load[dest_controller]['pktin']+=cost#预估目标控制器迁移完成后的负载是多少

                variance=np.var([load['pktin'] for load in controller_load.values()])#预估迁移完成后的集群负载均衡度
                #======计算迁移代价包括迁走的交换机数量、以及当前迁移方案中交换机对于控制器的影响，成本指标
                #影响的值为交换机的百分比综合
                cost_plan[plan_order]={
                    "dest_controller":dest_controller,
                    "migration_set":switches_migration_set,
                    "variance":variance,
                    "set_len":len(switches_migration_set)
                }
                plan_order+=1

        return cost_plan

    @staticmethod
    def build_load_matrix(plan):
        #构建负载因子矩阵
        """
        plan={1：{
                dest_controller:目标控制器
                migration_set：迁移集合
                variance:负载均衡度
                set_len:集合长度
                set_percentage:集合占比影响
                }....
        }
        矩阵的行序号就是交换机迁移集合序号
        """
        matrix=np.zeros((len(plan),2))#
        for order, p in plan.items():
            o = order
            matrix[o, 0] = p.get('variance')
            matrix[o, 1] =p.get('set_len')
        return matrix

    def update_controller_to_switches(self,src:int,dst:int,m_set:List):
        if src==dst:
            return
        for sw in m_set:
            self.controller_to_switches[src].remove(sw)
            #不需要向目的控制器的交换机列表添加迁移交换机，因为迁移交换机在迁移过后会触发目的控制器的sw_register事件，会自动注册

    def update_switches(self,src:int,dst:int,m_set:List):
        if src==dst:
            return
        for sw in m_set:
            self.switches[sw]=dst
        
    def update_switches_pktin_load(self,src:int,dst:int,m_set:List):
        if src==dst:
            return
        #body={sw1:{pktin_speed:xxx,percentage：xxx,pktin_size:xxx}....}}
        to_zero = {
            "pktin_speed" : 0,
            "pktin_size" : 0,
            "percentage" : "{}%".format(0.0)
        }
        for sw in m_set:
            
            self.switches_pktin_load[dst][sw]=self.switches_pktin_load[src][sw]
            
            self.switches_pktin_load[src][sw]=to_zero
            
    def update_global(self,**kwargs):
        #更新全局消息
        src=kwargs['src_controller']#源控制器
        dst=kwargs['dst_controller']#目的控制器
        m_set=kwargs['m_set']#迁移的交换机，处于源控制下的交换机
        # 更改控制器-交换机的映射
        self.update_controller_to_switches(src=src,dst=dst,m_set=m_set)
        #更改全局switches
        self.update_switches(src=src,dst=dst,m_set=m_set)
        #更新switches_pktin_load
        self.update_switches_pktin_load(src=src,dst=dst,m_set=m_set)

    def start_migration_plan(self,src_controller:str,plan:dict):
        #开始交换机迁移,首先更新全局关系
        dst_controller=plan.get("dest_controller")#目的控制器

        m_set=plan.get("migration_set")#迁移的交换机集合
        
        self.update_global(src_controller = src_controller, dst_controller = dst_controller, m_set = m_set)  # 更新拓扑关系
        
        dest_controller_ip,dest_controller_port=settings.CONTROLLER_IP,settings.CONTROLLER_PORTS[
                                                    settings.CONTROLLERS.index(f'c{dst_controller-1}')]

        commands=[f'ovs-vsctl set-controller s{switch} tcp:{dest_controller_ip}:{dest_controller_port}'
                  for switch in m_set]

        processes = [subprocess.Popen(cmd, shell=True) for cmd in commands]

        # 等待交换机迁移完成
        for process in processes:
            process.wait()
        
    def vikor_strategy(self,controller):
        switches_pkt_load=deepcopy(self.switches_pktin_load[controller])#控制器下所有交换机的负载
        switches=deepcopy(self.controller_to_switches[controller])#控制器的掌管的交换机
        dest_controller=self.adjacency_controller[controller]#领接控制器作为迁移的目的控制器
        cluster_avg_load=self.get_avg_load()#集群的平均负载
        controller_load=self.controller_pktin_load[controller].get('pktin')#控制器的负载
        assert controller_load>=cluster_avg_load #指定该值控制器的负载必须大于平均负载
        balance_load=round(controller_load-cluster_avg_load,4)#保留四位小数

        migration_plan=spawn(self.search_migration_plan,controller=controller,balance_load=balance_load,
                                                  dest_controller=dest_controller,
                                                  switches_pkt_load=switches_pkt_load,
                                                  switches=switches).get()
        if not migration_plan:
            self.log.warning("没有合适的迁移方案")
            return

        plan_with_cost=spawn(self.estimate_cost,plan=migration_plan,controller=controller).get()#评估方案

        #构建因子矩阵
        load_matrix=self.build_load_matrix(plan=plan_with_cost)
        
        #vikor决策
        vikor=Vikor(load_matrix=load_matrix)

        plan_order=vikor.vikor()
        final_migration_plan=plan_with_cost[plan_order]
        utils.display_migration_plan(controller,final_migration_plan)
        spawn(self.start_migration_plan,src_controller=controller,plan=final_migration_plan)#开始迁移
    
    def csm_strategy(self,controller):#随机迁移策略
        switches=self.controller_to_switches[controller]#控制器的掌管的交换机
        dest_controller = self.adjacency_controller[controller]  # 领接控制器作为迁移的目的控制器
        plan={
            "dest_controller":random.choice(dest_controller),
            "migration_set":[random.choice(switches)]
        }
        utils.display_migration_plan(controller, plan)
        spawn(self.start_migration_plan,src_controller=controller,plan=plan)
        
    def lsm_strategy(self,controller):#最轻迁移策略
        sw = ""
        min_load = 0xffffffff
        switches_pkt_load = deepcopy(self.switches_pktin_load[controller])  # 控制器下所有交换机的负载
        dest_controller = self.adjacency_controller[controller]  # 领接控制器作为迁移的目的控制器
        for k, v in switches_pkt_load.items() :
            load = v.get('pktin_speed')
            if min_load > load > 20 :
                min_load = load
                sw = k
        dst=dest_controller[np.argmin([self.get_controller_load(c) for c in dest_controller])]

        plan = {
            "dest_controller" : dst,
            "migration_set" : [sw]
        }
        utils.display_migration_plan(controller, plan)
        spawn(self.start_migration_plan, src_controller = controller, plan = plan)
    
    def save_cluster_data(self):
        
        spawn(Save_Switches_Status(self.switches_pktin_load))
        
        spawn(Save_Controller_Status(self.controller_pktin_load))
        
        spawn(Save_Switches_Map(self.controller_to_switches))
        
        
        
    def monitor(self):
        while 1:
            gevent.sleep(SAVE_DATA_PERIOD)
            #self.balance_check()
            self.save_cluster_data()
            

def main():
    server=Server()
    server.start()
    accept=Thread(target=server.accept_client)#客户端接收线程
    #db=Thread(target=server.save_data)#数据库存储线程
    #db.start()
    accept.start()

if __name__ == '__main__':
    main()#单进程