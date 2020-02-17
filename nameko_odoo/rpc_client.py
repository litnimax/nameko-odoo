import logging
from nameko.extensions import DependencyProvider
from .connection import OdooConnection

logger = logging.getLogger(__name__)


class OdooClient(DependencyProvider):
    connection = OdooConnection()

    def get_dependency(self, worker_ctx):
        return self.connection.odoo
