# Command Responder (GET/GETNEXT)
# Based on examples from http://pysnmp.sourceforge.net/

import logging
import os
from datetime import datetime

from pysnmp.entity import config
from pysnmp.entity.rfc3413 import context
from pysnmp.carrier.asynsock.dgram import udp
from pysnmp.entity import engine
from pysnmp.smi import builder
import gevent
from gevent import socket

from conpot.snmp import conpot_cmdrsp
from conpot.snmp.udp_server import DatagramServer


logger = logging.getLogger(__name__)


class SNMPDispatcher(DatagramServer):
    def __init__(self, log_queue):
        self.log_queue = log_queue
        self.__timerResolution = 0.5

    def registerRecvCbFun(self, recvCbFun):
        self.recvCbFun = recvCbFun

    def handle(self, msg, address):
        self.log(msg, address, 'in')
        self.recvCbFun(self, self.transportDomain, address, msg)

    def registerTransport(self, tDomain, transport):
        DatagramServer.__init__(self, transport, self.handle)
        self.transportDomain = tDomain

    def registerTimerCbFun(self, timerCbFun, tickInterval=None):
        pass

    def log(self, msg, address, direction):
        #TODO: log snmp request/response at the same time.

        if direction == 'in':
            log_key = 'request'
        else:
            log_key = 'response'

        #raw data (all snmp version)
        self.log_queue.put(
            {'remote': address,
             'timestamp': datetime.utcnow(),
             'data_type': 'snmp',
             'data': {
                 0: {log_key: msg.encode('hex')}
             }}
        )

    def sendMessage(self, outgoingMessage, transportDomain, transportAddress):
        self.log(outgoingMessage, transportAddress, 'out')
        self.socket.sendto(outgoingMessage, transportAddress)

    def getTimerResolution(self):
        return self.__timerResolution


class CommandResponder(object):
    def __init__(self, host, port, log_queue, mibpath):

        self.log_queue = log_queue
        # Create SNMP engine
        self.snmpEngine = engine.SnmpEngine()

        #path to custom mibs
        mibBuilder = self.snmpEngine.msgAndPduDsp.mibInstrumController.mibBuilder
        mibSources = mibBuilder.getMibSources() + (builder.DirMibSource(mibpath),)
        mibBuilder.setMibSources(*mibSources)

        # Transport setup
        udp_sock = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_DGRAM)
        udp_sock.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_BROADCAST, 1)
        udp_sock.bind((host, port))
        # UDP over IPv4
        self.addSocketTransport(
            self.snmpEngine,
            udp.domainName,
            udp_sock
        )

        #SNMPv1
        config.addV1System(self.snmpEngine, 'public-read', 'public')

        # SNMPv3/USM setup
        # user: usr-md5-des, auth: MD5, priv DES
        config.addV3User(
            self.snmpEngine, 'usr-md5-des',
            config.usmHMACMD5AuthProtocol, 'authkey1',
            config.usmDESPrivProtocol, 'privkey1'
        )
        # user: usr-sha-none, auth: SHA, priv NONE
        config.addV3User(
            self.snmpEngine, 'usr-sha-none',
            config.usmHMACSHAAuthProtocol, 'authkey1'
        )
        # user: usr-sha-aes128, auth: SHA, priv AES/128
        config.addV3User(
            self.snmpEngine, 'usr-sha-aes128',
            config.usmHMACSHAAuthProtocol, 'authkey1',
            config.usmAesCfb128Protocol, 'privkey1'
        )

        # Allow full MIB access for each user at VACM
        config.addVacmUser(self.snmpEngine, 1, 'public-read', 'noAuthNoPriv',
                           (1, 3, 6, 1, 2, 1))
        config.addVacmUser(self.snmpEngine, 3, 'usr-md5-des', 'authPriv',
                           (1, 3, 6, 1, 2, 1), (1, 3, 6, 1, 2, 1))
        config.addVacmUser(self.snmpEngine, 3, 'usr-sha-none', 'authNoPriv',
                           (1, 3, 6, 1, 2, 1), (1, 3, 6, 1, 2, 1))
        config.addVacmUser(self.snmpEngine, 3, 'usr-sha-aes128', 'authPriv',
                           (1, 3, 6, 1, 2, 1), (1, 3, 6, 1, 2, 1))

        # Get default SNMP context this SNMP engine serves
        snmpContext = context.SnmpContext(self.snmpEngine)

        # Register SNMP Applications at the SNMP engine for particular SNMP context
        conpot_cmdrsp.c_GetCommandResponder(self.snmpEngine, snmpContext)
        conpot_cmdrsp.c_SetCommandResponder(self.snmpEngine, snmpContext)
        conpot_cmdrsp.c_NextCommandResponder(self.snmpEngine, snmpContext)
        conpot_cmdrsp.c_BulkCommandResponder(self.snmpEngine, snmpContext)

    def addSocketTransport(self, snmpEngine, transportDomain, transport):
        """Add transport object to socket dispatcher of snmpEngine"""
        if not snmpEngine.transportDispatcher:
            snmpEngine.registerTransportDispatcher(SNMPDispatcher(self.log_queue))
        snmpEngine.transportDispatcher.registerTransport(transportDomain, transport)

    def register(self, mibname, symbolname, value):
        self.snmpEngine.msgAndPduDsp.mibInstrumController.mibBuilder.loadModules(mibname)
        s = self._get_mibSymbol(mibname, symbolname)
        logger.info('Registered: {0}'.format(s))

        MibScalarInstance, = self.snmpEngine.msgAndPduDsp.mibInstrumController.mibBuilder.importSymbols('SNMPv2-SMI',
                                                                                                        'MibScalarInstance')

        x = MibScalarInstance(s.name, (0,), s.syntax.clone(value))
        self.snmpEngine.msgAndPduDsp.mibInstrumController.mibBuilder.exportSymbols(mibname, x)

    def _get_mibSymbol(self, mibname, symbolname):
        modules = self.snmpEngine.msgAndPduDsp.mibInstrumController.mibBuilder.mibSymbols
        if mibname in modules:
            if symbolname in modules[mibname]:
                return modules[mibname][symbolname]

    def serve_forever(self):
        self.snmpEngine.transportDispatcher.serve_forever()

    def stop(self):
        self.snmpEngine.transportDispatcher.stop_accepting()


if __name__ == "__main__":
    server = CommandResponder()
    print 'Starting echo server on port 161'
    server.serve_forever()
