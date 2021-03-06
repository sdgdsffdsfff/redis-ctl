import gevent
import gevent.monkey

gevent.monkey.patch_all()

import time
import logging
import threading
import random
from socket import error as SocketError
from influxdb.client import InfluxDBClientError
from algalon_cli import AlgalonClient

import config
import file_ipc
import handlers.base
import models.base
import stats.db


CACHING_NODES = {}


def _load_from(cls, nodes):
    r = []
    for n in nodes:
        if (n['host'], n['port']) in CACHING_NODES:
            cache_node = CACHING_NODES[(n['host'], n['port'])]
            r.append(cache_node)
            cache_node.suppress_alert = n.get('suppress_alert')
            continue
        loaded_node = cls.get_by(n['host'], n['port'])
        CACHING_NODES[(n['host'], n['port'])] = loaded_node
        loaded_node.suppress_alert = n.get('suppress_alert')
        loaded_node.balance_plan = n.get('balance_plan')
        r.append(loaded_node)
    return r


class Poller(threading.Thread):
    def __init__(self, nodes, algalon_client):
        threading.Thread.__init__(self)
        self.daemon = True
        self.nodes = nodes
        logging.debug('Poller %x distributed %d nodes',
                      id(self), len(self.nodes))
        self.algalon_client = algalon_client

    def run(self):
        with handlers.base.app.app_context():
            for node in self.nodes:
                node = node.reattach()
                logging.debug('Poller %x collect for %s:%d',
                              id(self), node['host'], node['port'])
                node.collect_stats(self._emit_data, self._send_alarm)
                node.add_to_db()

            models.base.db.session.commit()

    def _send_alarm(self, message, trace):
        if self.algalon_client is not None:
            self.algalon_client.send_alarm(message, trace)

    def _emit_data(self, json_body):
        try:
            stats.db.client.write_points(json_body)
        except (SocketError, InfluxDBClientError, StandardError), e:
            logging.exception(e)


def run(interval, algalon_client, app):
    from daemonutils import node_polling
    from daemonutils.cluster_task import TaskPoller

    TaskPoller(app, interval).start()

    NODES_EACH_THREAD = 20
    with handlers.base.app.app_context():
        while True:
            poll = file_ipc.read_poll()
            nodes = _load_from(node_polling.RedisNodeStatus, poll['nodes'])
            proxies = _load_from(node_polling.ProxyStatus, poll['proxies'])
            models.base.db.session.commit()

            all_nodes = nodes + proxies
            random.shuffle(all_nodes)
            pollers = [
                Poller(all_nodes[i: i + NODES_EACH_THREAD], algalon_client)
                for i in xrange(0, len(all_nodes), NODES_EACH_THREAD)]
            for p in pollers:
                p.start()
            time.sleep(interval)

            for p in pollers:
                p.join()

            logging.info('Total %d nodes, %d proxies', len(nodes),
                         len(proxies))

            try:
                file_ipc.write_details({n.addr: n.details for n in nodes},
                                       {p.addr: p.details for p in proxies})
            except StandardError, e:
                logging.exception(e)


def main():
    config.init_logging()

    app = handlers.base.app
    app.config['SQLALCHEMY_DATABASE_URI'] = config.SQLALCHEMY_DATABASE_URI
    import daemonutils.node_polling
    models.base.init_db(app)

    if config.INFLUXDB and config.INFLUXDB['host']:
        stats.db.init(**config.INFLUXDB)
    algalon_client = (AlgalonClient(**config.ALGALON)
                      if config.ALGALON and config.ALGALON['dsn'] else None)
    run(config.POLL_INTERVAL, algalon_client, app)

if __name__ == '__main__':
    main()
