import logging
from nameko.extensions import DependencyProvider
import odoorpc
import requests
from urllib.error import URLError


logger = logging.getLogger(__name__)


class OdooClient(DependencyProvider):
    odoo = None

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
        self.verify_certificate = self.container.config.get(
                                            'ODOO_VERIFY_CERTIFICATE', False)
        self.setup_rpc_session()
        self.http_session = requests.Session()

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

