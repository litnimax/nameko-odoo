import logging
from nameko.extensions import DependencyProvider
from .connection import OdooConnection

logger = logging.getLogger(__name__)


class OdooClient(DependencyProvider):
    connection = OdooConnection()

    def get_dependency(self, worker_ctx):
        # A little hack to inject event into service.
        worker_ctx.service.odoo_connected = self.connection.connected
        return self.connection.odoo
