# Copyright 2015 Cisco Systems, Inc.
# All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""BGP Protocol"""

import logging
import traceback
import struct
import time

import netaddr
from oslo.config import cfg
from twisted.internet import protocol

from yabgp.common import constants as bgp_cons
from yabgp.message.open import Open
from yabgp.message.keepalive import KeepAlive
from yabgp.message.update import Update
from yabgp.message.notification import Notification
from yabgp.message.route_refresh import RouteRefresh
from yabgp.common import exception as excep

LOG = logging.getLogger(__name__)

CONF = cfg.CONF


class BGP(protocol.Protocol):
    """Protocol class for BGP 4"""

    def __init__(self):

        """Create a BGP protocol.
        """
        self.fsm = None
        self.peer_id = None

        self.disconnected = False
        self.receive_buffer = b''
        self.fourbytesas = False

        # statistic
        self.msg_sent_stat = {
            'Opens': 0,
            'Notifications': 0,
            'Updates': 0,
            'Keepalives': 0,
            'RouteRefresh': 0
        }
        self.msg_recv_stat = {
            'Opens': 0,
            'Notifications': 0,
            'Updates': 0,
            'Keepalives': 0,
            'RouteRefresh': 0
        }

    def connectionMade(self):

        """
        Starts the initial negotiation of the protocol
        """

        # Set transport socket options
        self.transport.setTcpNoDelay(True)
        # set tcp option if you want
        #  self.transport.getHandle().setsockopt(socket.IPPROTO_TCP, TCP_MD5SIG, md5sig)

        LOG.info("[%s]TCP Connection established", self.factory.peer_addr)

        # Set the local BGP id from the local IP address if it's not set
        if self.factory.bgp_id is None:
            try:
                self.factory.bgp_id = int(netaddr.IPAddress(self.transport.getHost().host))
            except Exception as e:
                LOG.error(e)
                error_str = traceback.format_exc()
                LOG.debug(error_str)
                self.factory.bgp_id = int(netaddr.IPAddress('127.0.0.1'))
        try:
            self.fsm.connection_made()
        except Exception as e:
            LOG.error(e)
            error_str = traceback.format_exc()
            LOG.debug(error_str)

    def connectionLost(self, reason):

        """Called when the associated connection was lost.

        :param reason: the reason of lost connection.
        """
        LOG.debug('Called connectionLost')
        # Don't do anything if we closed the connection explicitly ourselves
        if self.disconnected:
            self.factory.connection_closed(self)
            LOG.info('Connection lost return and do nothing')
            return

        LOG.info("[%s]Connection lost:%s", self.factory.peer_addr, reason.getErrorMessage())

        try:
            # tell FSM that TCP connection is lost.
            self.fsm.connection_failed()
        except Exception as e:
            LOG.error(e)
            error_str = traceback.format_exc()
            LOG.debug(error_str)

    def dataReceived(self, data):

        """
        Appends newly received data to the receive buffer, and
        then attempts to parse as many BGP messages as possible.

        :param data: the data received from TCP buffer.
        """

        # Buffer possibly incomplete data first
        self.receive_buffer += data
        while self.parse_buffer():
            pass

    def parse_buffer(self):
        """
        Parse TCP buffer data.

        :return: True or False
        """
        buf = self.receive_buffer

        if len(buf) < bgp_cons.HDR_LEN:
            # Every BGP message is at least 19 octets. Maybe the rest
            # hasn't arrived yet.
            return False

        # Check whether the first 16 octets of the buffer consist of
        # the BGP marker (all bits one)
        if buf[:16] != 16 * b'\xff':
            self.fsm.header_error(bgp_cons.ERR_MSG_HDR_CONN_NOT_SYNC)
            return False
            # Parse the BGP header
        try:
            marker, length, msg_type = struct.unpack('!16sHB', buf[:bgp_cons.HDR_LEN])
        except Exception as e:
            LOG.error(e)
            error_str = traceback.format_exc()
            LOG.debug(error_str)
            self.fsm.header_error(bgp_cons.ERR_MSG_HDR_CONN_NOT_SYNC)
            return False
            # Check the length of the message, must be less than 4096, bigger than 19
        if length < bgp_cons.HDR_LEN or length > bgp_cons.MAX_LEN:
            self.fsm.header_error(bgp_cons.ERR_MSG_HDR_BAD_MSG_LEN, struct.pack('!H', length))
            # Check whether the entire message is already available
        if len(buf) < length:
            return False
        msg = buf[bgp_cons.HDR_LEN:length]
        t = time.time()  # the time when received that packet.
        try:
            if msg_type == bgp_cons.MSG_OPEN:
                try:
                    self.open_received(timestamp=t, msg=msg)
                except excep.MessageHeaderError as e:
                    LOG.error(e)
                    self.fsm.header_error(suberror=e.sub_error)
                    return False
                except excep.OpenMessageError as e:
                    LOG.error(e)
                    self.fsm.open_message_error(suberror=e.sub_error)
                    return False

            elif msg_type == bgp_cons.MSG_UPDATE:
                self.update_received(timestamp=t, msg=msg)

            elif msg_type == bgp_cons.MSG_NOTIFICATION:
                self.notification_received(Notification().parse(msg))

            elif msg_type == bgp_cons.MSG_KEEPALIVE:
                try:
                    self.keepalive_received(timestamp=t, msg=msg)
                except excep.MessageHeaderError as e:
                    LOG.error(e)
                    self.fsm.header_error(suberror=e.sub_error)
                    return False
            elif msg_type == bgp_cons.MSG_ROUTEREFRESH:
                route_refresh_msg = RouteRefresh().parse(msg)
                self.route_refresh_received(msg=route_refresh_msg)
            elif msg_type == bgp_cons.MSG_CISCOROUTEREFRESH:
                route_refresh_msg = RouteRefresh().parse(msg)
                self.route_refresh_received(msg=route_refresh_msg)
            else:
                # unknown message type
                self.fsm.header_error(bgp_cons.ERR_MSG_HDR_BAD_MSG_TYPE, struct.pack('!H', msg_type))
        except Exception as e:
            LOG.error(e)
            error_str = traceback.format_exc()
            LOG.debug(error_str)
        self.receive_buffer = self.receive_buffer[length:]
        return True

    def closeConnection(self):

        """Close the connection"""

        if self.transport.connected:
            self.transport.loseConnection()
            self.disconnected = True

    def update_received(self, timestamp, msg):

        """Called when a BGP Update message was received."""
        result = Update().parse([timestamp, self.fourbytesas, msg])
        if result['sub_error']:
            # Get address family
            if result['nlri'] or result['withdraw']:
                afi_safi = bgp_cons.AFI_SAFI_STR_DICT['ipv4']
            else:
                afi_safi = (0, 0)
                # write message to file
            self.factory.write_msg(
                timestamp=result['time'],
                msg_type=6,
                msg={
                    'attr': result['attr'],
                    'nlri': result['nlri'],
                    'withdraw': result['withdraw']
                },
                afi_safi=afi_safi,
                flush=True
            )
            LOG.error('[%s] Update message error: sub error=%s', self.factory.peer_addr, result['sub_error'])
        else:
            # no error
            # get address family
            if result['nlri'] or result['withdraw']:
                afi_safi = bgp_cons.AFI_SAFI_STR_DICT['ipv4']
            elif bgp_cons.BGPTYPE_MP_REACH_NLRI in result['attr']:
                afi_safi = result['attr'][bgp_cons.BGPTYPE_MP_REACH_NLRI]['afi_safi']
            elif bgp_cons.BGPTYPE_MP_UNREACH_NLRI in result['attr']:
                afi_safi = result['attr'][bgp_cons.BGPTYPE_MP_UNREACH_NLRI]['afi_safi']
            else:
                afi_safi = (0, 0)
            msg = {
                'attr': result['attr'],
                'nlri': result['nlri'],
                'withdraw': result['withdraw']
            }
            self.factory.write_msg(
                timestamp=result['time'],
                msg_type=bgp_cons.MSG_UPDATE,
                msg=msg,
                afi_safi=afi_safi,
                flush=True
            )
            if not CONF.standalone:
                self.factory.channel.send_message(exchange='', routing_key=self.factory.peer_addr, message=str(msg))

        self.msg_recv_stat['Updates'] += 1
        self.fsm.update_received()

    def send_update(self, msg):
        """
        send update message to the peer
        :param msg: message dictionary
        :return:
        """
        try:
            msg_update = Update().construct(msg, self.fourbytesas)
            self.transport.write(msg_update)
            self.msg_sent_stat['Updates'] += 1
            return True
        except Exception as e:
            LOG.error(e)
            return False

    def send_notification(self, error, sub_error, data=b''):
        """
        send BGP notification message

        :param error:
        :param sub_error:
        :param data:
        :return:
        """
        self.msg_sent_stat['Notifications'] += 1
        LOG.info(
            "[%s]Send a BGP Notification message to the peer [Error: %s, Suberror: %s, Error data: %s ]",
            self.factory.peer_addr, error, sub_error, repr(data))
        # message statistic
        self.msg_sent_stat['Notifications'] += 1
        # construct message
        msg_notification = Notification().construct(error, sub_error, data)
        # send message
        self.transport.write(msg_notification)

    def notification_received(self, msg):
        """
        BGP notification message received.
        """
        self.msg_recv_stat['Notifications'] += 1
        LOG.info(
            '[%s]Notification message received, error=%s, sub error=%s, data=%s',
            self.factory.peer_addr, msg[0], msg[1], msg[2])
        nofi_msg = {'Error': msg[0], 'Suberror': msg[1], 'Error data': repr(msg[2])}
        self.factory.write_msg(
            timestamp=time.time(),
            msg_type=3,
            msg=nofi_msg,
            afi_safi=(0, 0),
            flush=True
        )
        self.fsm.notification_received(msg[0], msg[1])

        LOG.debug('offline')

    def send_keepalive(self):
        """
        send BGP keepalive message.
        """
        self.msg_sent_stat['Keepalives'] += 1
        LOG.info("[%s]Send a BGP KeepAlive message to the peer.", self.factory.peer_addr)
        # message statistci
        # self.msg_sent_stat['Keepalives'] += 1
        # construct message
        msg_keepalive = KeepAlive().construct()
        # send message
        self.transport.write(msg_keepalive)

    def keepalive_received(self, timestamp, msg):
        """
        process keepalive message

        :param timestamp:
        :param msg:
        :return:
        """
        self.msg_recv_stat['Keepalives'] += 1
        LOG.info("[%s]A BGP KeepAlive message was received from peer.", self.factory.peer_addr)
        KeepAlive().parse(msg)

        if CONF.message.write_keepalive:
            # write bgp message
            self.factory.write_msg(
                timestamp=timestamp,
                msg_type=4,
                msg=None,
                afi_safi=(0, 0),
                flush=True
            )
        self.fsm.keep_alive_received()

    def capability_negotiate(self):
        """
        Open message capability negotiation
        :return:
        """
        # if received open message from remote peer firstly
        # then copy peer's capability to local according to the
        # local support. best effort support.
        if cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']:
            for capability in cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['local']:
                if capability not in cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']:
                    cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['local'].pop(capability)

    def send_open(self):
        """
        send open message

        :return:
        """
        # construct Open message
        self.capability_negotiate()
        open_msg = Open(version=bgp_cons.VERSION, asn=self.factory.my_asn, hold_time=self.fsm.hold_time,
                        bgp_id=self.factory.bgp_id). \
            construct(cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['local'])
        # send message
        self.transport.write(open_msg)
        self.msg_sent_stat['Opens'] += 1
        LOG.info("[%s]Send a BGP Open message to the peer.", self.factory.peer_addr)
        LOG.info("[%s]Probe's Capabilities:", self.factory.peer_addr)
        for key in cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['local']:
            LOG.info("--%s = %s", key, cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['local'][key])

    def open_received(self, timestamp, msg):
        """
        porcess open message

        :param timestamp: timestamp that received this message
        :param msg: binary raw message data
        :return:
        """

        self.msg_recv_stat['Opens'] += 1
        open_msg = Open()
        parse_result = open_msg.parse(msg)
        if self.fsm.bgp_peering.peer_asn != open_msg.asn:
            raise excep.OpenMessageError(sub_error=bgp_cons.ERR_MSG_OPEN_BAD_PEER_AS)

        # Open message Capabilities negotiation
        cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote'] = open_msg.capa_dict

        LOG.info("[%s]A BGP Open message was received", self.factory.peer_addr)
        LOG.info('--version = %s', open_msg.version)
        LOG.info('--ASN = %s', open_msg.asn)
        LOG.info('--hold time = %s', open_msg.hold_time)
        LOG.info('--id = %s', open_msg.bgp_id)
        LOG.info("[%s]Neighbor's Capabilities:", self.factory.peer_addr)
        for key in cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']:
            if key == 'four_bytes_as':
                self.fourbytesas = True
            LOG.info("--%s = %s", key, cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote'][key])

        # write bgp message
        self.factory.write_msg(
            timestamp=timestamp,
            msg_type=1,
            msg=parse_result,
            afi_safi=(0, 0),
            flush=True
        )
        self.peer_id = open_msg.bgp_id
        self.bgp_peering.set_peer_id(open_msg.bgp_id)

        self.negotiate_hold_time(open_msg.hold_time)
        self.fsm.open_received()

    def send_route_refresh(self, afi, safi, res=0):
        """
        Send bgp route refresh message
        :param afi: address family
        :param safi: sub address family
        :param res: reserve, default is 0
        """
        # check if the peer support route refresh
        if cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']['cisco_route_refresh']:
            type_code = bgp_cons.MSG_CISCOROUTEREFRESH
        elif cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']['route_refresh']:
            type_code = bgp_cons.MSG_ROUTEREFRESH
        else:
            return False
        # check if the peer support this address family
        if (afi, safi) not in cfg.CONF.bgp.running_config[self.factory.peer_addr]['capability']['remote']['afi_safi']:
            return False
        # construct message
        msg_routerefresh = RouteRefresh(afi, safi, res).construct(type_code)
        # send message
        self.transport.write(msg_routerefresh)
        self.msg_sent_stat['RouteRefresh'] += 1
        LOG.info("[%s]Send BGP RouteRefresh message to the peer.", self.factory.peer_addr)
        return True

    def route_refresh_received(self, msg):
        """
        Route Refresh message received.

        :param msg: msg content
        """
        LOG.info(
            '[%s]Route Refresh message received, afi=%s, res=%s, safi=%s',
            self.factory.peer_addr, msg[0], msg[1], msg[2])

    def negotiate_hold_time(self, hold_time):

        """Negotiates the hold time"""

        self.fsm.hold_time = min(self.fsm.hold_time, hold_time)
        if self.fsm.hold_time != 0 and self.fsm.hold_time < 3:
            self.fsm.open_message_error(bgp_cons.ERR_MSG_OPEN_UNACCPT_HOLD_TIME)
            # Derived times
        self.fsm.keep_alive_time = self.fsm.hold_time / 3
        LOG.info(
            "[%s]Hold time:%s,Keepalive time:%s", self.factory.peer_addr,
            self.fsm.hold_time, self.fsm.keep_alive_time)
