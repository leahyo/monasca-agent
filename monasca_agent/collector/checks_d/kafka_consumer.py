import collections
import logging
import monasca_agent.collector.checks as checks

try:
    import kafka.client as client
    import kafka.common as common
    import kafka.consumer as consumer
except ImportError:
    raise Exception('Missing python dependency: kafka (https://github.com/mumrah/kafka-python)')


log = logging.getLogger(__name__)


class KafkaConnection(object):
    """ A simple context manager for kafka connections """

    def __init__(self, connect_str):
        self.connect_str = connect_str

    def __enter__(self):
        self.kafka_conn = client.KafkaClient(self.connect_str)
        return self.kafka_conn

    def __exit__(self, type, value, traceback):
        try:
            self.kafka_conn.close()
        except Exception:
            log.exception('Error cleaning up Kafka connection')


class KafkaCheck(checks.AgentCheck):
    """ Checks the configured kafka instance reporting the consumption lag
        for each partition per topic in each consumer group. If full_output
        is set also reports broker offsets and the current consumer offset.
        Works on Kafka version >= 0.8.1.1
    """
    def _validate_consumer_groups(self, val):
        """ Private config validation/marshalling functions
        """
        try:
            consumer_group, topic_partitions = val.items()[0]
            assert isinstance(consumer_group, (str, unicode))
            topic, partitions = topic_partitions.items()[0]
            assert isinstance(topic, (str, unicode))
            assert isinstance(partitions, (list, tuple))
            return val
        except Exception as e:
            self.log.exception(e)
            raise Exception('''The `consumer_groups` value must be a mapping of mappings, like this:
consumer_groups:
  myconsumer0: # consumer group name
    mytopic0: [0, 1] # topic: list of partitions
  myconsumer1:
    mytopic0: [0, 1, 2]
    mytopic1: [10, 12]
''')

    def _get_kafka_offsets(self, kafka_conn, consumer_groups):

        # Query Kafka for consumer offsets
        consumer_offsets = {}
        topics = collections.defaultdict(set)
        for consumer_group, topic_partitions in consumer_groups.iteritems():
            for topic, partitions in topic_partitions.iteritems():
                kafka_consumer = consumer.SimpleConsumer(kafka_conn, consumer_group, topic)
                if len(partitions) == 0:  # Use all found partitions if none are specified.
                    try:
                        partitions = kafka_consumer.offsets.keys()
                    except Exception:
                        kafka_consumer.stop()
                        self.log.error('Error fetching partition list for topic {0}'.format(topic))
                        continue

                # Remember the topic partitions encountered so that we can look up their broker offsets later
                topics[topic].update(set(partitions))
                consumer_offsets[(consumer_group, topic)] = {}
                for partition in partitions:
                    try:
                        consumer_offsets[(consumer_group, topic)][partition] = kafka_consumer.offsets[partition]
                    except KeyError:
                        kafka_consumer.stop()
                        self.log.error('Error fetching consumer offset for {0} partition {1}'.format(topic, partition))
                kafka_consumer.stop()

        # Query Kafka for the broker offsets, done in a separate loop so only one query is done
        # per topic/partition even if multiple consumer groups watch the same topic
        broker_offsets = {}
        for topic, partitions in topics.iteritems():
            offset_responses = []
            for p in partitions:
                try:
                    response = kafka_conn.send_offset_request([common.OffsetRequest(topic, p, -1, 1)])
                    offset_responses.append(response[0])
                except common.KafkaError as e:
                    self.log.error("Error fetching broker offset: {0}".format(e))

            for resp in offset_responses:
                broker_offsets[(resp.topic, resp.partition)] = resp.offsets[0]

        return consumer_offsets, broker_offsets

    def check(self, instance):
        consumer_groups = self.read_config(instance, 'consumer_groups',
                                           cast=self._validate_consumer_groups)
        kafka_host_ports = self.read_config(instance, 'kafka_connect_str')
        full_output = self.read_config(instance, 'full_output', cast=bool, optional=True)
        per_partition = self.read_config(instance, 'per_partition', cast=bool, optional=True)
        if not per_partition:
            full_output = False
        dimensions = {'component': 'kafka', 'service': 'kafka'}

        # Connect to Kafka and pull information
        with KafkaConnection(kafka_host_ports) as kafka_conn:
            consumer_offsets, broker_offsets = self._get_kafka_offsets(kafka_conn, consumer_groups)

        # Report the broker data if full output
        if full_output:
            broker_dimensions = dimensions.copy()
            for (topic, partition), broker_offset in broker_offsets.iteritems():
                broker_dimensions.update({'topic': topic, 'partition': partition})
                broker_offset = broker_offsets.get((topic, partition))
                self.gauge('kafka.broker_offset', broker_offset,
                           dimensions=self._set_dimensions(broker_dimensions, instance))

        # Report the consumer data
        consumer_dimensions = dimensions.copy()
        for (consumer_group, topic), offsets in consumer_offsets.iteritems():
            if per_partition:
                for partition, consumer_offset in offsets.iteritems():
                    # Get the broker offset
                    broker_offset = broker_offsets.get((topic, partition))
                    # Report the consumer offset and lag
                    consumer_dimensions.update({'topic': topic, 'partition': partition,
                                                'consumer_group': consumer_group})
                    if full_output:
                        self.gauge('kafka.consumer_offset', consumer_offset,
                                   dimensions=self._set_dimensions(consumer_dimensions, instance))
                    self.gauge('kafka.consumer_lag', broker_offset - consumer_offset,
                               dimensions=self._set_dimensions(consumer_dimensions, instance))
            else:
                consumer_dimensions.update({'topic': topic, 'consumer_group': consumer_group})
                total_lag = 0
                for partition, consumer_offset in offsets.iteritems():
                    broker_offset = broker_offsets.get((topic, partition))
                    total_lag += broker_offset - consumer_offset

                self.gauge('kafka.consumer_lag', total_lag,
                           dimensions=self._set_dimensions(consumer_dimensions, instance))
