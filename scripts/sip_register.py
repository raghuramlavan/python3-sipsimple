#!/usr/bin/env python

import sys
import re
import traceback
import os
import signal
import random
from thread import start_new_thread, allocate_lock
from Queue import Queue
from optparse import OptionParser, OptionValueError
import dns.resolver
from application.configuration import *
from application.process import process
from pypjua import *

re_host_port = re.compile("^(?P<host>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:(?P<port>\d+))?$")
class SIPProxyAddress(tuple):
    def __new__(typ, value):
        match = re_host_port.search(value)
        if match is None:
            raise ValueError("invalid IP address/port: %r" % value)
        if match.group("port") is None:
            port = 5060
        else:
            port = match.group("port")
            if port > 65535:
                raise ValueError("port is out of range: %d" % port)
        return match.group("host"), port


class AccountConfig(ConfigSection):
    _datatypes = {"username": str, "domain": str, "password": str, "display_name": str, "outbound_proxy": SIPProxyAddress}
    username = None
    domain = None
    password = None
    display_name = None
    outbound_proxy = None, None


process._system_config_directory = os.path.expanduser("~")
configuration = ConfigFile("pypjua.ini")
configuration.read_settings("Account", AccountConfig)

queue = Queue()
packet_count = 0
start_time = None
user_quit = True
lock = allocate_lock()

def event_handler(event_name, **kwargs):
    global start_time, packet_count, queue
    if event_name == "siptrace":
        if start_time is None:
            start_time = kwargs["timestamp"]
        packet_count += 1
        if kwargs["received"]:
            direction = "RECEIVED"
        else:
            direction = "SENDING"
        buf = ["%s: Packet %d, +%s" % (direction, packet_count, (kwargs["timestamp"] - start_time))]
        buf.append("%(timestamp)s: %(source_ip)s:%(source_port)d --> %(destination_ip)s:%(destination_port)d" % kwargs)
        buf.append(kwargs["data"])
        queue.put(("print", "\n".join(buf)))
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))

def read_queue(e, username, domain, password, display_name, proxy_ip, proxy_port, expires, do_siptrace):
    global user_quit, lock, queue
    lock.acquire()
    try:
        if proxy_ip is None:
            # for now assume 1 SRV record and more than one A record
            srv_answers = dns.resolver.query("_sip._udp.%s" % domain, "SRV")
            a_answers = dns.resolver.query(str(srv_answers[0].target), "A")
            route = Route(random.choice(a_answers).address, srv_answers[0].port)
        else:
            route = Route(proxy_ip, proxy_port or 5060)
        credentials = Credentials(SIPURI(user=username, host=domain, display=display_name), password)
        reg = Registration(credentials, route=route, expires=expires)
        print 'Registering for SIP address "%s" at proxy %s:%d' % (credentials.uri, route.host, route.port)
        reg.register()
        while True:
            command, data = queue.get()
            if command == "print":
                print data
            if command == "pypjua_event":
                event_name, args = data
                if event_name == "Registration_state":
                    if args["state"] == "registered":
                        print "REGISTER was succesfull"
                        print "Contact: %s" % args["contact_uri"]
                        if len(args["contact_uri_list"]) > 1:
                            print "Other registered contacts: %s" % ", ".join([contact_uri for contact_uri in args["contact_uri_list"] if contact_uri != args["contact_uri"]])
                    elif args["state"] == "unregistered":
                        if args["code"] / 100 != 2:
                            print "Unregistered: %(code)d %(reason)s" % args
                        user_quit = False
                        command = "quit"
            if command == "eof":
                reg.unregister()
            if command == "quit":
                break
    except:
        user_quit = False
        traceback.print_exc()
    finally:
        e.stop()
        if not user_quit:
            os.kill(os.getpid(), signal.SIGINT)
        lock.release()

def do_register(**kwargs):
    global user_quit, lock, queue
    print "Using configuration file %s" % process.config_file("pypjua.ini")
    ctrl_d_pressed = False
    e = Engine(event_handler, do_siptrace=kwargs["do_siptrace"], auto_sound=False)
    e.start()
    start_new_thread(read_queue, (e,), kwargs)
    try:
        while True:
            try:
                raw_input()
            except EOFError:
                if not ctrl_d_pressed:
                    queue.put(("eof", None))
                    ctrl_d_pressed = True
    except KeyboardInterrupt:
        if user_quit:
            print "CTRL+C pressed, exiting instantly!"
            queue.put(("quit", True))
        lock.acquire()
        return

def parse_host_port(option, opt_str, value, parser, host_name, port_name, default_port):
    match = re_host_port.match(value)
    if match is None:
        raise OptionValueError("Could not parse supplied address: %s" % value)
    setattr(parser.values, host_name, match.group("host"))
    if match.group("port") is None:
        setattr(parser.values, port_name, default_port)
    else:
        setattr(parser.values, port_name, int(match.group("port")))

def parse_options():
    retval = {}
    description = "This example script will register the provided SIP account and refresh it while the program is running. When CTRL+D is pressed it will unregister."
    usage = "%prog [options]"
    default_options = dict(expires=300, proxy_ip=AccountConfig.outbound_proxy[0], proxy_port=AccountConfig.outbound_proxy[1], username=AccountConfig.username, password=AccountConfig.password, domain=AccountConfig.domain, display_name=AccountConfig.display_name, do_siptrace=False)
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.set_defaults(**default_options)
    parser.add_option("-u", "--username", type="string", dest="username", help="Username to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-d", "--domain", type="string", dest="domain", help="SIP domain to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-p", "--password", type="string", dest="password", help="Password to use to authenticate the local account. This overrides the setting from the config file.")
    parser.add_option("-n", "--display-name", type="string", dest="display_name", help="Display name to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-e", "--expires", type="int", dest="expires", help='"Expires" value to set in REGISTER. Default is 300 seconds.')
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=lambda option, opt_str, value, parser: parse_host_port(option, opt_str, value, parser, "proxy_ip", "proxy_port", 5060), help="Outbound SIP proxy to use. By default a lookup is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="do_siptrace", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    options, args = parser.parse_args()
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    return retval

def main():
    do_register(**parse_options())

if __name__ == "__main__":
    main()
