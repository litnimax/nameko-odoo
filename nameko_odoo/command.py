import eventlet
import logging
from nameko.standalone.rpc import ClusterRpcProxy
from nameko.exceptions import RpcTimeout
import os
import sys

logger = logging.getLogger(__name__)


class Command:
    def __init__(self, cls):
        self.cls = cls

    def ping(self, channel, message):
        uid = message.get('notify_uid')
        if uid:
            try:
                self.cls.connection.odoo.env['bus.bus'].sendone(
                    'remote_agent_notification_{}'.format(uid),
                    {'message': 'Ping reply', 'title': 'Remote Agent'})
            except Exception:
                logger.exception('Ping error:')

    def restart(self, channel, message):
        logger.info('Restart command received.')
        args = sys.argv[:]
        uid = message.get('notify_uid')
        if uid:
            channel = 'remote_agent_notification_{}'.format(uid)
            self.cls.connection.odoo.env['remote_agent.agent'].bus_sendone(
                channel, {'message': 'Restarted.'})
        os.execv(sys.executable, [sys.executable] + args)

    def nameko_rpc(self, channel, message):
        logger.debug('Channel %s nameko RPC message: %s', channel,
                     str(message)[:512])
        if message.get('delay'):
            logger.debug('Delay %s', message['delay'])
            eventlet.sleep(float(message['delay']))
        # Handle Nameko RPC request sent from Odoo and return the result.
        result = {}
        # Return back data sent by caller.
        if message.get('pass_back'):
            result['pass_back'] = message['pass_back']
        service_name = message['service']
        method = message['method']
        args = message.get('args', ())
        kwargs = message.get('kwargs', {})
        callback_model = message.get('callback_model')
        callback_method = message.get('callback_method')
        try:
            timeout = float(message.get('timeout', '3'))
            with ClusterRpcProxy(
                    self.cls.container.config, timeout=timeout) as cluster_rpc:
                service = getattr(cluster_rpc, service_name)
                ret = getattr(service, method)(*args, **kwargs)
                result['result'] = ret
        except RpcTimeout:
            logger.warning('[NAMEKO_RPC_TIMEOUT] %s.%s timeout: %s',
                           service_name, method, timeout)
            result.setdefault(
                'error', {})['message'] = 'Service {} {} timeout.'.format(
                service_name, method)
        except Exception as e:
            logger.exception('[NAMEKO_RPC_ERROR]')
            result.setdefault('error', {})['message'] = str(e)
        finally:
            if callback_model and callback_method:
                try:
                    logger.debug('Execute %s.%s.',
                                 callback_model, callback_method)
                    self.cls.connection.odoo.execute(
                        callback_model, callback_method, result)
                except Exception:
                    logger.exception('[CONNECTION.ODOO_RPC_ERROR]')
            # Check if we shoud send status report
            if message.get('status_notify_uid'):
                uid = message['status_notify_uid']
                try:
                    logger.debug('Status notify to %s.', uid)
                    error = result.get('error', {}).get('message')
                    title = message['method'].replace('_', ' ').capitalize()
                    status = error if error else 'Success'
                    self.cls.connection.odoo.execute(
                        'bus.bus', 'sendone',
                        'remote_agent_notification_{}'.format(uid),
                        {
                            'message': status,
                            'title': title,
                            'level': 'warning' if error else 'info',
                        })
                except Exception:
                    logger.exception('[CONNECTION.ODOO_RPC_ERROR]')
            else:
                logger.debug('No callback model / method specified.')
