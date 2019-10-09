import logging
import uuid
from nameko.extensions import DependencyProvider
from nameko.dependency_providers import Config
from nameko.rpc import rpc
import odoorpc
import requests
from urllib2 import URLError


logger = logging.getLogger(__name__)


class OdooClient(DependencyProvider):
    odoo = None

    def setup(self):
        self.odoo_host = self.container.config['ODOO_HOST']
        self.odoo_port = self.container.config['ODOO_PORT']
        self.odoo_user = self.container.config['ODOO_USER']
        self.odoo_pass = self.container.config['ODOO_PASS']
        self.odoo_protocol = self.container.config['ODOO_PROTOCOL']
        self.odoo_db = self.container.config['ODOO_DB']
        self.setup_session()

    def setup_session(self):
        try:
            logger.info(
                    'Connecting to Odoo at %s://%s:%s',
                    self.odoo_protocol, self.odoo_host, self.odoo_port)
            odoo = odoorpc.ODOO(self.odoo_host, port=self.odoo_port,
                                protocol=self.odoo_protocol)
            odoo.login(self.odoo_db, self.odoo_user, self.odoo_pass)
            logger.info('Connected to Odoo as %s', self.odoo_user)
            self.odoo = odoo
            
        except odoorpc.error.RPCError as e:
            if 'res.users()' in str(e):
                logger.error(
                        'Odoo login %s not found or bad password %s, '
                        'check in Odoo!',
                        self.odoo_user, self.odoo_pass)
            else:
                logger.exception('RPC error:')
        except URLError as e:
            logger.error(e)
        except Exception as e:
            if 'Connection refused' in repr(e):
                logger.error('Odoo refusing connection.')
            else:
                logger.exception(e)


    def select_db(self):
        if self.odoo_session_db_selected:
            return
        logger.debug('Selecting Odoo database (session refresh)')
        auth_url = '{}://{}:{}/web/session/authenticate'.format(
                self.odoo_scheme, self.odoo_host, self.odoo_polling_port)
        data = {
            'jsonrpc': '2.0',
            'params': {
                'context': {},
                'db': self.odoo_db,
                'login': self.odoo_login,
                'password': self.odoo_password,
            },
        }
        headers = {
            'Content-type': 'application/json'
        }
        #req = Request('POST', url, data=json.dumps(data), headers=headers)
        rep = self.odoo_session.post(
                         auth_url,
                         verify=self.https_verify_cert,
                         data=json.dumps(data),
                         headers=headers)
        result = rep.json()
        if rep.status_code != 200 or result.get('error'):
            logger.error(u'Odoo authenticate error {}: {}'.format(
                                    rep.status_code,
                                    json.dumps(result['error'], indent=2)))
        else:
            logger.info('Odoo authenticated for long polling')
        self.odoo_session_db_selected = True


    def odoo_bus_poll(self):
        if not self.bus_enabled:
            logger.info('Odoo bus poll is disabled')
            return
        self.odoo_connected.wait()
        # Send 1-st message that will be omitted
        try:
            self.odoo.env[self.agent_model].send_agent(
                                self.agent_uid,
                                json.dumps({
                                    'message': 'ping',
                                    'random_sleep': '0'}))
        except Exception as e:
            logger.exception('First ping error:')
        last = 0
        while True:
            try:
                bus_url = '{}://{}:{}/longpolling/poll'.format(
                    self.odoo_scheme, self.odoo_host, self.odoo_polling_port)
                channel = '{}/{}'.format(self.agent_channel, self.agent_uid)
                # Select DB first
                self.select_db()
                # Now let try to poll
                logger.debug('Polling %s at %s',
                             channel, bus_url)
                r = self.odoo_session.post(
                            bus_url,
                            timeout=self.odoo_bus_timeout,
                            verify=self.odoo_verify_cert,
                            headers={'Content-Type': 'application/json'},
                            json={
                                'params': {
                                    'last': last,
                                    'channels': [channel]}})
                if self.bus_trace:
                    logger.debug('Bus trace: %s', r.text)
                try:
                    r.json()
                except ValueError:
                    logger.error('JSON parse bus reply error: %s', r.text)
                result = r.json().get('result')
                if not result:
                    error = r.json().get('error')
                    if error:
                        logger.error(json.dumps(error, indent=2))
                        gevent.sleep(self.odoo_reconnect_seconds)
                        continue
                if last == 0:
                    # Ommit queued data
                    for msg in result:
                        if type(msg['message']) != dict:
                            message = json.loads(msg['message'])
                        else:
                            message = msg['message']
                        if not message.get('message'):
                            logger.error('No Message attribute in message: %s',
                                         message)
                            continue
                        logger.debug('Ommit bus message %s', message['message'])
                        last = msg['id']
                    continue

                for msg in result:
                    last = msg['id']
                    if type(msg['message']) != dict:
                        message = json.loads(msg['message'])
                    else:
                        message = msg['message']
                    if not message.get('message'):
                        logger.error('No Message attribute in message: %s',
                                     message)
                        continue
                    logger.debug('Handle bus message %s', message['message'])
                    gevent.spawn(self.handle_bus_message,
                                 msg['channel'], msg['message'])

            except Exception as e:
                no_wait = False
                if isinstance(e, requests.ConnectionError):
                    if 'Connection aborted' in str(e.message):
                        logger.warning('Connection aborted')
                    elif 'Connection refused' in str(e.message):
                        logger.warning('Connection refused')
                    else:
                        logger.warning(e.message)
                elif isinstance(e, requests.HTTPError):
                    logger.warning(r.reason)
                elif isinstance(e, requests.ReadTimeout):
                    no_wait = True
                    logger.warning('Bus poll timeout, re-polling')
                else:
                    logger.exception('Bus error:')
                if not no_wait:
                    gevent.sleep(self.odoo_reconnect_seconds)


    def handle_bus_message(self, channel, msg):
        if not type(msg) == dict:
            msg = json.loads(msg)
        if self.token != msg.pop('token', None):
            logger.warning(
                        'Channel %s token mismatch, ignore message: %s', msg)
            return
        # Check for RPC message
        if msg['message'] == 'rpc':
            logger.debug('RPC message received: %s', msg['data'])
            self.bus_rpc_requests.put((msg.get('reply_channel'), msg['data']))
        else:
            result = self.handle_message(channel, msg)
            if result and msg.get('reply_channel'):
                self.odoo.env[self.agent_model].bus_sendone(
                                            msg['reply_channel'], result)


    def notify_user(self, uid, message, title='Agent',
                    warning=False, sticky=False):
        self.odoo_connected.wait()
        if not uid:
            logger.debug(u'No uid, will not notify')
            return
        logger.debug('Notify user %s: %s', uid, message)
        self.odoo.env[self.agent_model].bus_sendone(
                                 'remote_agent_notification_{}'.format(uid), {
                                    'message': message,
                                    'warning': warning,
                                    'sticky': sticky,
                                    'title': title})



