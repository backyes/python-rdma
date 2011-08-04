# Copyright 2011 Obsidian Research Corp. GPLv2, see COPYING.
from __future__ import with_statement

import atexit
import os
import rdma
import rdma.IBA as IBA
import rdma.madtransactor
import rdma.tools
import select
import socket
import struct
import time

SIM_SERVER_HOST = os.environ.get('IBSIM_SERVER_NAME', 'localhost')
SIM_SERVER_PORT = int(os.environ.get('IBSIM_SERVER_PORT', '7070'))
SIM_HOST = os.environ.get('SIM_HOST') # node ID

SIM_MAGIC = 0xdeadbeef
SIM_CTL_ERROR = 0		# reply type
SIM_CTL_CONNECT = 1
SIM_CTL_DISCONNECT = 2
SIM_CTL_GET_PORT = 3
SIM_CTL_GET_VENDOR = 4
SIM_CTL_GET_GID = 5
SIM_CTL_GET_GUID = 6
SIM_CTL_GET_NODEINFO = 7
SIM_CTL_GET_PORTINFO = 8
SIM_CTL_SET_ISSM = 9
SIM_CTL_GET_PKEYS = 10

# The server does no byte ordering ... client must have same endianness as server
_struct_ctl = struct.Struct('=LLLL64s') # sim_ctl
_struct_info = struct.Struct('=LLL32s') # sim_client_info
_struct_request = struct.Struct('>HHLLLQ 256s') # sim_request

conn = None

class SimConnection(object):
    def __init__(self, nodeid=None, qp=0, issm=0):
        if nodeid is None:
            nodeid = ''

        a = socket.getaddrinfo(SIM_SERVER_HOST, SIM_SERVER_PORT,
                               socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_IP)[0]

        self._pkt_sock = socket.socket(a[0], a[1], socket.IPPROTO_IP)
        self._pkt_sock.bind(('', 0))
        self._client_id = self._pkt_sock.getsockname()[1]

        self._ctl_sock = socket.socket(a[0], a[1], socket.IPPROTO_IP)
        self._ctl_sock.bind(('', 0))
        self._ctl_sock.connect(a[4])

        resp = self._ctl(SIM_CTL_CONNECT,
                         _struct_info.pack(self._client_id, qp, issm, nodeid))
        info = _struct_info.unpack(resp[4][:_struct_info.size])
        self._client_id = info[0]

        addr = a[4][0]
        port = SIM_SERVER_PORT + self._client_id + 1
        self._pkt_sock.connect((addr, port))

    def __del__(self):
        self._ctl(SIM_CTL_DISCONNECT);

    def _ctl(self, type, data=None):
        if data is None:
            data = ''
        msg = _struct_ctl.pack(SIM_MAGIC, self._client_id, type, len(data), data)
        self._ctl_sock.send(msg)
        resp = self._ctl_sock.recv(_struct_ctl.size)
        resp = _struct_ctl.unpack(resp)
        return resp

class SimDevice(object):
    def __init__(self, name):
        self.name = name
        info = conn._ctl(SIM_CTL_GET_NODEINFO)
        self.node_info = IBA.SMPNodeInfo()
        self.node_info.unpack_from(info[4])
        self.fw_ver = 0
        self.node_desc = '' # FIXME

#        import rdma.IBA_describe
#        import sys
#        rdma.IBA_describe.struct_dump(sys.stdout, self.node_info)

        if self.node_info.nodeType == IBA.NODE_SWITCH:
            begin = 0
            end = 1
        else:
            begin = 1
            end = self.node_info.numPorts + 1
        self.end_ports = rdma.KeyList((i, SimEndPort(self, i)) for i in range(begin,end))

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def hca_type(self):
        return 'Simulator'

    @property
    def node_type(self):
        return self.node_info.nodeType

    @property
    def node_guid(self):
        return self.node_info.nodeGUID

    @property
    def sys_image_guid(self):
        return self.node_info.systemImageGUID

    @property
    def board_id(self):
        return self.node_info.deviceID

    @property
    def hw_ver(self):
        return self.node_info.revision

    def __str__(self):
        return self.name

class SimEndPort(object):
    def __init__(self,parent,port_id):
        self.parent = parent
        self.port_id = port_id
        self._port_info = IBA.SMPPortInfo()
        self._port_info_age = 0
        self.pkeys = ( 0xFFFF, )
        self._gids = None

    def _get(self, field):
        now = time.time()
        if now > self._port_info_age + 1:
            info = conn._ctl(SIM_CTL_GET_PORTINFO, struct.pack('B', self.port_id))
            self._port_info.unpack_from(info[4])
            self._port_info_age = now

        return getattr(self._port_info, field)

    def umad(self):
        return SimUMAD(self)

    @property
    def lid(self):
        return self._get('LID')

    @property
    def lmc(self):
        return self._get('LMC')

    @property
    def state(self):
        return self._get('portState')

    @property
    def phys_state(self):
        return self._get('portPhysicalState')

    @property
    def rate(self):
        widths = { 1: '1X', 2: '4X', 4: '8X', 8: '12X' }
        speeds = { 1: '2.5 Gb/sec', 2: '5 Gb/sec', 4: '10 Gb/sec' }
        w = self._get('linkWidthActive')
        s = self._get('linkSpeedActive')
        return '%s (%s)' % (widths.get(w, '??'), speeds.get(s, '??'))

    @property
    def cap_mask(self):
        return self._get('capabilityMask')

    @property
    def sm_lid(self):
        return self._get('masterSMLID')

    @property
    def sm_sl(self):
        return self._get('masterSMSL')

    @property
    def port_guid(self):
        return rdma.devices._conv_gid2guid(self.gids[0])

    @property
    def gids(self):
        if self._gids is None:
            prefix = self._get('GIDPrefix')

            # FIXME: simulator reports node guid only.
            # The following conversion into port guid relies on convention:
            guid = bytearray(8)
            self.parent.node_guid.pack_into(guid)
            guid[7] = self.port_id
            guid = str(guid)

            default_gid = IBA.GID(prefix=IBA.GID_DEFAULT_PREFIX, guid=guid)
            if prefix == IBA.GID_DEFAULT_PREFIX:
                self._gids = (default_gid,)
            else:
                self._gids = (IBA.GID(prefix, guid=guid), default_gid)
        return self._gids

    @property
    def default_gid(self):
        return self.gids[0]

    @property
    def subnet_timeout(self):
        try:
            # This is only available through verbs so for now we have
            # verbs set it when it gets it..
            return self._cached_subnet_timeout;
        except AttributeError:
            # Otherwise use the default
            return 18;

    def pkey_index(self,pkey):
        """Return the ``pkey index`` for pkey value *pkey*."""
        return self.pkeys.index(pkey);

    @property
    def sa_path(self):
        """The path to the SA. This path should only be used for GMPs of class
        :data:`~rdma.IBA.MAD_SUBNET_ADMIN` and it should never be changed.
        See IBA 15.4.2."""
        try:
            return self._cached_sa_path
        except AttributeError:
            pass;

        try:
            pkey_idx = self.pkey_index(IBA.PKEY_DEFAULT);
        except ValueError:
            try:
                pkey_idx = self.pkey_index(IBA.PKEY_PARTIAL_DEFAULT);
            except ValueError:
                raise rdma.RDMAError("Could not find the SA default PKey");

        self._cached_sa_path = rdma.path.IBPath(self,DLID=self.sm_lid,
                                                SLID=self.lid,
                                                SL=self.sm_sl,dqpn=1,sqpn=1,
                                                qkey=IBA.IB_DEFAULT_QP1_QKEY,
                                                pkey_index=pkey_idx,
                                                packet_life_time=self.subnet_timeout);
        return self._cached_sa_path;

    def __str__(self):
        return "%s/%u"%(self.parent,self.port_id)


class SimUMAD(rdma.madtransactor.MADTransactor):
    def __init__(self,parent):
        global conn
        rdma.madtransactor.MADTransactor.__init__(self)
        self._poll = select.poll()
        self._poll.register(conn._pkt_sock, select.POLLIN)
        self.end_port = parent
        self.parent = parent
        self._tid = int(os.urandom(4).encode("hex"),16);

    def _get_new_TID(self):
        self._tid = (self._tid + 1) % (1 << 32);
        return self._tid;

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def close(self):
        pass

    def sendto(self,buf,path,agent_id=None):
        global conn
        req = _struct_request.pack(path.DLID, path.SLID, path.dqpn, path.sqpn, 0,
                                   len(buf), str(buf))
        conn._pkt_sock.send(req)

    def recvfrom(self,wakeat):
        global conn
        timeout = wakeat - rdma.tools.clock_monotonic();
        if timeout <= 0 or not self._poll.poll(timeout*1000):
            return None
        resp = conn._pkt_sock.recv(512)
        dlid, slid, dqpn, sqpn, status, length, pkt = _struct_request.unpack(resp)
        path = rdma.path.IBPath(self.end_port)
        path.DLID = dlid
        path.SLID = slid
        path.DQPN = dqpn
        path.SQPN = sqpn
        return (bytearray(pkt),path)

    def _execute(self,buf,path,sendOnly=False):
        self.sendto(buf,path)
        if sendOnly:
            return None;

        rmatch = self._get_reply_match_key(buf);
        expire = path.mad_timeout + rdma.tools.clock_monotonic();
        retries = path.retries;
        while True:
            ret = self.recvfrom(expire);
            if ret is None:
                if retries == 0:
                    return None;
                retries = retries - 1;
                self._execute(buf,path,True);

                expire = path.mad_timeout + rdma.tools.clock_monotonic();
                continue;
            elif rmatch == self._get_match_key(ret[0]):
                return ret;
            else:
                if self.trace_func is not None:
                    self.trace_func(self,rdma.madtransactor.TRACE_UNEXPECTED,
                                    path=path,ret=ret);

if os.environ.get('IBSIM_SERVER_NAME'):
    conn = SimConnection(SIM_HOST)
    def finish():
        global conn
        del conn

    atexit.register(finish)