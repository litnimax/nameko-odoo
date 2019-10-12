import eventlet
import json
import logging
from nameko.extensions import SharedExtension, ProviderCollector, Entrypoint
from nameko.extensions import DependencyProvider
import odoorpc
import requests
from urllib.error import URLError


logger = logging.getLogger(__name__)


class OdooClientExtension(DependencyProvider):
    """
    Helper class to get the odoo client from worker instance.
    """
    def get_dependency(self, worker_ctx):
        for ext in self.container.extensions:
            if isinstance(ext, OdooClient):
                return ext



class OdooClient(SharedExtension, ProviderCollector):
    odoo = None
    db_selected = False
    http_session = None # Requests session for bus polling
    bus_channels = {} # Channels to poll bus on.


    def get_dependency(self, worker_ctx):
        return self.odoo

    def setup(self):
        # Must be defined settings
        self.odoo_host = self.container.config['ODOO_HOST']
        self.odoo_port = self.container.config['ODOO_PORT']
        self.odoo_user = self.container.config['ODOO_USER']
        self.odoo_pass = self.container.config['ODOO_PASS']
        self.odoo_db = self.container.config['ODOO_DB']
        # Optional settings, use defaults.
        # See OdooRPC protocol: 'jsonrpc' or jsonrpc+ssl'
        self.odoo_protocol = self.container.config.get(
                                                'ODOO_PROTOCOL', 'jsonrpc')
        self.odoo_scheme = self.container.config.get(
                                                'ODOO_SCHEME', 'http')
        self.bus_enabled = self.container.config.get('ODOO_BUS_ENABLED', True)
        self.bus_polling_port = self.container.config.get(
                                                'ODOO_BUS_POLLING_PORT', 8072)
        self.bus_timeout = self.container.config.get(
                                            'ODOO_BUS_TIMEOUT', 55)
        self.bus_trace = self.container.config.get(
                                            'ODOO_BUS_TRACE', False)
        self.single_db = self.container.config.get('ODOO_SINGLE_DB', False)
        self.verify_certificate = self.container.config.get(
                                            'ODOO_VERIFY_CERTIFICATE', False)
        self.setup_rpc_session()
        self.http_session = requests.Session()        
        self.container.spawn_managed_thread(self.poll_bus)

    def setup_rpc_session(self):
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
        """
        For multi database Odoo setup it is required to first select a database
        to work with.
        But if you have single db setup or use db_filters so that always one db
        is selected set ODOO_SINGLE_DB=yes.
        """
        if self.db_selected:
            return
        logger.debug('Selecting Odoo database (session refresh)')
        auth_url = '{}://{}:{}/web/session/authenticate'.format(
                self.odoo_scheme, self.odoo_host, self.bus_polling_port)
        data = {
            'jsonrpc': '2.0',
            'params': {
                'context': {},
                'db': self.odoo_db,
                'login': self.odoo_user,
                'password': self.odoo_pass,
            },
        }
        headers = {
            'Content-type': 'application/json'
        }
        rep = self.http_session.post(
                         auth_url,
                         verify=self.verify_certificate,
                         data=json.dumps(data),
                         headers=headers)
        result = rep.json()
        if rep.status_code != 200 or result.get('error'):
            logger.error(u'Odoo authenticate error {}: {}'.format(
                                    rep.status_code,
                                    json.dumps(result['error'], indent=2)))
        else:
            logger.info('Odoo authenticated for long polling')
        self.db_selected = True


    def setup_bus_channels(self):
        # Here we track bus message consumers.
        for provider in self._providers:
            if provider.channel not in self.bus_channels.keys():
                self.bus_channels[provider.channel] = []
            logger.debug(
                'Adding Odoo bus listener for channel %s', provider.channel)
            self.bus_channels[provider.channel].append(provider.handle_message)


    def poll_bus(self):
        """
        Odoo bus poller to get massages from Odoo and route the to corresponding
        services.
        """
        if not self.bus_enabled:
            logger.info('Odoo bus poll is not enabled, not using /longpolling/poll.')
            return
        last = 0
        while True:
            try:
                bus_url = '{}://{}:{}/longpolling/poll'.format(
                    self.odoo_scheme, self.odoo_host, self.bus_polling_port)                
                # Select DB first
                if not self.single_db:
                    self.select_db()
                # Now let try to poll
                logger.debug('Polling %s at %s',
                             list(self.bus_channels.keys()), bus_url)
                r = self.http_session.post(
                            bus_url,
                            timeout=self.bus_timeout,
                            verify=self.verify_certificate,
                            headers={'Content-Type': 'application/json'},
                            json={
                                'params': {
                                    'last': last,
                                    'channels': list(
                                                self.bus_channels.keys())}})
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
                        eventlet.sleep(1)
                        continue
                if last == 0:
                    # Ommit queued data
                    for msg in result:
                        logger.debug('Ommit bus message %s', msg)
                        last = msg['id']
                    continue

                for msg in result:
                    last = msg['id']
                    logger.debug('Handle bus message %s', msg)
                    self.handle_bus_message(msg['channel'], msg['message'])

            except Exception as e:
                no_wait = False
                if isinstance(e, requests.exceptions.ConnectionError):
                    if 'Connection aborted' in str(e):
                        logger.warning('Odoo Connection aborted')
                    elif 'Failed to establish' in str(e):
                        logger.warning('Odoo Connection refused')
                    else:
                        logger.warning(e.strerror)
                elif isinstance(e, requests.exceptions.HTTPError):
                    logger.warning(r.reason)
                elif isinstance(e, requests.exceptions.ReadTimeout):
                    no_wait = True
                    logger.warning('Bus poll timeout, re-polling')
                else:
                    logger.exception('Bus error:')
                if not no_wait:
                    eventlet.sleep(1)


    def handle_bus_message(self, channel, message):
        # Get provieers and pass the message
        if self.bus_channels.get(channel):
            for handler in self.bus_channels[channel]:
                handler(message)
    
    def notify_user(self, uid, message, title='Notification',
                    level='info', sticky=False):
        # Helper func used from services to sent Odoo user notifications.
        if not uid:
            logger.debug('No uid, will not notify')
            return
        logger.debug('Notify user %s: %s', uid, message)
        self.bus_sendone('notify_{}_{}'.format(level, uid),
                         {'message': message,
                                   'sticky': sticky,
                                   'title': title})

    def bus_sendone(self, channel, message):
        self.odoo.env['bus.bus'].sendone(channel, message)


class BusEventHandler(Entrypoint):
    odoo_client = OdooClient()

    def __init__(self, channel, **kwargs):
        self.channel = channel
        super(BusEventHandler, self).__init__(**kwargs)

    def setup(self):
        self.odoo_client.register_provider(self)
        self.odoo_client.setup_bus_channels()

    def stop(self):
        self.odoo_client.unregister_provider(self)

    def handle_message(self, event):
        args = (event, )
        kwargs = {}
        context_data = {}
        self.container.spawn_worker(self, args, kwargs,
                                    context_data=context_data,
                                    handle_result=self.handle_result)

    def handle_result(self, worker_ctx, result, exc_info):
        logger.debug('Result message: %s, result: %s, exc: %s',
                     worker_ctx, result, exc_info)
        original_message = worker_ctx.args[0]
        if 'reply_channel' in original_message:
            try:            
                self.odoo_client.bus_sendone(
                                original_message['reply_channel'], result)
            except Exception as e:
                logger.error('Could not sent result to Odoo: %s', e)
        return result, exc_info

bus = BusEventHandler.decorator
