import json
import os
import re
import sys
import traceback
from datetime import datetime
from time import sleep

import requests
import snowflake.connector
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic, ConfigResource, NewPartitions
from confluent_kafka.avro import AvroProducer

import test_suit
from test_suit.test_at_least_once_semantic import TestAtLeastOnceSemantic
from test_suit.test_exactly_once_semantic import TestExactlyOnceSemantic
from test_suit.test_exactly_once_semantic_time_based import TestExactlyOnceSemanticTimeBased
from test_suit.test_utils import parsePrivateKey, RetryableError


def errorExit(message):
    print(datetime.now().strftime("%H:%M:%S "), message)
    exit(1)


class KafkaTest:
    def __init__(self, kafkaAddress, schemaRegistryAddress, kafkaConnectAddress, credentialPath, testVersion, enableSSL,
                 snowflakeCloudPlatform, enableDeliveryGuaranteeTests=False):
        self.testVersion = testVersion
        self.credentialPath = credentialPath
        # can be None or one of AWS, AZURE, GCS
        self.snowflakeCloudPlatform = snowflakeCloudPlatform
        # default is false or set to true as env variable
        self.enableDeliveryGuaranteeTests = enableDeliveryGuaranteeTests
        with open(self.credentialPath) as f:
            credentialJson = json.load(f)
            testHost = credentialJson["host"]
            testUser = credentialJson["user"]
            testDatabase = credentialJson["database"]
            testSchema = credentialJson["schema"]
            testWarehouse = credentialJson["warehouse"]
            pk = credentialJson["encrypted_private_key"]
            pk_passphrase = credentialJson["private_key_passphrase"]

        self.TEST_DATA_FOLDER = "./test_data/"
        self.httpHeader = {'Content-type': 'application/json', 'Accept': 'application/json'}

        self.SEND_INTERVAL = 0.01  # send a record every 10 ms
        self.VERIFY_INTERVAL = 60  # verify every 60 secs
        self.MAX_RETRY = 30  # max wait time 30 mins
        self.MAX_FLUSH_BUFFER_SIZE = 5000  # flush buffer when 10000 data was in the queue

        self.kafkaConnectAddress = kafkaConnectAddress
        self.schemaRegistryAddress = schemaRegistryAddress
        self.kafkaAddress = kafkaAddress

        if enableSSL:
            print(datetime.now().strftime("\n%H:%M:%S "), "=== Enable SSL ===")
            self.client_config = {
                "bootstrap.servers": kafkaAddress,
                "security.protocol": "SASL_SSL",
                "ssl.ca.location": "./crts/ca-cert",
                "sasl.mechanism": "PLAIN",
                "sasl.username": "client",
                "sasl.password": "client-secret"
            }
        else:
            self.client_config = {
                "bootstrap.servers": kafkaAddress
            }

        self.adminClient = AdminClient(self.client_config)
        self.producer = Producer(self.client_config)
        sc_config = self.client_config
        sc_config['schema.registry.url'] = schemaRegistryAddress
        self.avroProducer = AvroProducer(sc_config)

        reg = "[^\/]*snowflakecomputing"  # find the account name
        account = re.findall(reg, testHost)
        if len(account) != 1 or len(account[0]) < 20:
            print(datetime.now().strftime("%H:%M:%S "),
                  "Format error in 'host' field at profile.json, expecting account.snowflakecomputing.com:443")

        pkb = parsePrivateKey(pk, pk_passphrase)
        self.snowflake_conn = snowflake.connector.connect(
            user=testUser,
            private_key=pkb,
            account=account[0][:-19],
            warehouse=testWarehouse,
            database=testDatabase,
            schema=testSchema
        )

    def msgSendInterval(self):
        # sleep self.SEND_INTERVAL before send the second message
        sleep(self.SEND_INTERVAL)

    def startConnectorWaitTime(self):
        sleep(10)

    def verifyWaitTime(self):
        # sleep two minutes before verify result in SF DB
        print(datetime.now().strftime("\n%H:%M:%S "),
              "=== Sleep {} secs before verify result in Snowflake DB ===".format(
                  self.VERIFY_INTERVAL), flush=True)
        sleep(self.VERIFY_INTERVAL)

    def verifyWithRetry(self, func, round):
        retryNum = 0
        while retryNum < self.MAX_RETRY:
            try:
                func(round)
                break
            except test_suit.test_utils.ResetAndRetry:
                retryNum = 0
                print(datetime.now().strftime("%H:%M:%S "), "=== Reset retry count and retry ===", flush=True)
            except test_suit.test_utils.RetryableError as e:
                retryNum += 1
                print(datetime.now().strftime("%H:%M:%S "), "=== Failed, retryable. {}===".format(e.msg), flush=True)
                self.verifyWaitTime()
            except test_suit.test_utils.NonRetryableError as e:
                print(datetime.now().strftime("\n%H:%M:%S "), "=== Non retryable error raised ===\n{}".format(e.msg),
                      flush=True)
                raise test_suit.test_utils.NonRetryableError()
            except snowflake.connector.errors.ProgrammingError as e:
                print("Error in VerifyWithRetry" + str(e))
                if e.errno == 2003:
                    retryNum += 1
                    print(datetime.now().strftime("%H:%M:%S "), "=== Failed, table not created ===", flush=True)
                    self.verifyWaitTime()
                else:
                    raise
        if retryNum == self.MAX_RETRY:
            print(datetime.now().strftime("\n%H:%M:%S "), "=== Max retry exceeded ===", flush=True)
            raise test_suit.test_utils.NonRetryableError()

    def createTopics(self, topicName, partitionNum=1, replicationNum=1):
        self.adminClient.create_topics([NewTopic(topicName, partitionNum, replicationNum)])

    def deleteTopic(self, topicName):
        deleted_topics = self.adminClient.delete_topics([topicName])
        for topic, f in deleted_topics.items():
            try:
                f.result()  # The result itself is None
                print("Topic deletion successful:{}".format(topic))
            except Exception as e:
                print("Failed to delete topic {}: {}".format(topicName, e))

    def describeTopic(self, topicName):
        configs = self.adminClient.describe_configs(
            resources=[ConfigResource(restype=ConfigResource.Type.TOPIC, name=topicName)])
        for config_resource, f in configs.items():
            try:
                configs = f.result()
                print("Topic {} config is as follows:".format(topicName))
                for key, value in configs.items():
                    print(key, ':', value)
            except Exception as e:
                print("Failed to describe topic {}: {}".format(topicName, e))

    def createPartitions(self, topicName, new_total_partitions):
        kafka_partitions = self.adminClient.create_partitions(
            new_partitions=[NewPartitions(topicName, new_total_partitions)])
        for topic, f in kafka_partitions.items():
            try:
                f.result()  # The result itself is None
                print("Topic {} partitions created".format(topic))
            except Exception as e:
                print("Failed to create topic partitions {}: {}".format(topic, e))

    def sendBytesData(self, topic, value, key=[], partition=0, headers=[]):
        if len(key) == 0:
            for i, v in enumerate(value):
                self.producer.produce(topic, value=v, partition=partition, headers=headers)
                if (i + 1) % self.MAX_FLUSH_BUFFER_SIZE == 0:
                    self.producer.flush()
        else:
            for i, (k, v) in enumerate(zip(key, value)):
                self.producer.produce(topic, value=v, key=k, partition=partition, headers=headers)
                if (i + 1) % self.MAX_FLUSH_BUFFER_SIZE == 0:
                    self.producer.flush()
        self.producer.flush()

    def sendAvroSRData(self, topic, value, value_schema, key=[], key_schema="", partition=0):
        if len(key) == 0:
            for i, v in enumerate(value):
                self.avroProducer.produce(
                    topic=topic, value=v, value_schema=value_schema, partition=partition)
                if (i + 1) % self.MAX_FLUSH_BUFFER_SIZE == 0:
                    self.producer.flush()
        else:
            for i, (k, v) in enumerate(zip(key, value)):
                self.avroProducer.produce(
                    topic=topic, value=v, value_schema=value_schema, key=k, key_schema=key_schema, partition=partition)
                if (i + 1) % self.MAX_FLUSH_BUFFER_SIZE == 0:
                    self.producer.flush()
        self.avroProducer.flush()

    def cleanTableStagePipe(self, connectorName, topicName="", partitionNumber=1):
        if topicName == "":
            topicName = connectorName
        tableName = topicName
        stageName = "SNOWFLAKE_KAFKA_CONNECTOR_{}_STAGE_{}".format(connectorName, topicName)

        print(datetime.now().strftime("\n%H:%M:%S "), "=== Drop table {} ===".format(tableName))
        self.snowflake_conn.cursor().execute("DROP table IF EXISTS {}".format(tableName))

        print(datetime.now().strftime("%H:%M:%S "), "=== Drop stage {} ===".format(stageName))
        self.snowflake_conn.cursor().execute("DROP stage IF EXISTS {}".format(stageName))

        for p in range(partitionNumber):
            pipeName = "SNOWFLAKE_KAFKA_CONNECTOR_{}_PIPE_{}_{}".format(connectorName, topicName, p)
            print(datetime.now().strftime("%H:%M:%S "), "=== Drop pipe {} ===".format(pipeName))
            self.snowflake_conn.cursor().execute("DROP pipe IF EXISTS {}".format(pipeName))

        print(datetime.now().strftime("%H:%M:%S "), "=== Done ===", flush=True)

    def verifyStageIsCleaned(self, connectorName, topicName=""):
        if topicName == "":
            topicName = connectorName
        stageName = "SNOWFLAKE_KAFKA_CONNECTOR_{}_STAGE_{}".format(connectorName, topicName)

        res = self.snowflake_conn.cursor().execute("list @{}".format(stageName)).fetchone()
        if res is not None:
            raise RetryableError("stage not cleaned up ")

    # validate content match gold regex
    def regexMatchOneLine(self, res, goldMetaRegex, goldContentRegex):
        meta = res[0].replace(" ", "").replace("\n", "")
        content = res[1].replace(" ", "").replace("\n", "")
        goldMetaRegex = "^" + goldMetaRegex.replace("\"", "\\\"").replace("{", "\\{").replace("}", "\\}") \
            .replace("[", "\\[").replace("]", "\\]").replace("+", "\\+") + "$"
        goldContentRegex = "^" + goldContentRegex.replace("\"", "\\\"").replace("{", "\\{").replace("}", "\\}") \
            .replace("[", "\\[").replace("]", "\\]").replace("+", "\\+") + "$"
        if re.search(goldMetaRegex, meta) is None:
            raise test_suit.test_utils.NonRetryableError("Record meta data:\n{}\ndoes not match gold regex "
                                                         "label:\n{}".format(meta, goldMetaRegex))
        if re.search(goldContentRegex, content) is None:
            raise test_suit.test_utils.NonRetryableError("Record content:\n{}\ndoes not match gold regex "
                                                         "label:\n{}".format(content, goldContentRegex))

    def updateConnectorConfig(self, fileName, connectorName, configMap):
        with open('./rest_request_generated/' + fileName + '.json') as f:
            c = json.load(f)
            config = c['config']
            for k in configMap:
                config[k] = configMap[k]
        requestURL = "http://{}/connectors/{}/config".format(self.kafkaConnectAddress, connectorName)
        r = requests.put(requestURL, json=config, headers=self.httpHeader)
        print(datetime.now().strftime("%H:%M:%S "), r, " updated connector config")

    def restartConnector(self, connectorName):
        requestURL = "http://{}/connectors/{}/restart".format(self.kafkaConnectAddress, connectorName)
        r = requests.post(requestURL, headers=self.httpHeader)
        print(datetime.now().strftime("%H:%M:%S "), r, " restart connector")

    def pauseConnector(self, connectorName):
        requestURL = "http://{}/connectors/{}/pause".format(self.kafkaConnectAddress, connectorName)
        r = requests.put(requestURL, headers=self.httpHeader)
        print(datetime.now().strftime("%H:%M:%S "), r, " pause connector")

    def resumeConnector(self, connectorName):
        requestURL = "http://{}/connectors/{}/resume".format(self.kafkaConnectAddress, connectorName)
        r = requests.put(requestURL, headers=self.httpHeader)
        print(datetime.now().strftime("%H:%M:%S "), r, " resume connector")

    def deleteConnector(self, connectorName):
        requestURL = "http://{}/connectors/{}".format(self.kafkaConnectAddress, connectorName)
        r = requests.delete(requestURL, headers=self.httpHeader)
        print(datetime.now().strftime("%H:%M:%S "), r, " delete connector")

    def closeConnector(self, fileName, nameSalt):
        snowflake_connector_name = fileName.split(".")[0] + nameSalt
        delete_url = "http://{}/connectors/{}".format(self.kafkaConnectAddress, snowflake_connector_name)
        print(datetime.now().strftime("\n%H:%M:%S "), "=== Delete connector {} ===".format(snowflake_connector_name))
        code = requests.delete(delete_url, timeout=10).status_code
        print(datetime.now().strftime("%H:%M:%S "), code)

    def createConnector(self, fileName, nameSalt):
        rest_template_path = "./rest_request_template"
        rest_generate_path = "./rest_request_generated"

        with open(self.credentialPath) as f:
            credentialJson = json.load(f)
            testHost = credentialJson["host"]
            testUser = credentialJson["user"]
            # required for Snowpipe Streaming
            testRole = credentialJson["role"]
            testDatabase = credentialJson["database"]
            testSchema = credentialJson["schema"]
            pk = credentialJson["private_key"]
            # Use Encrypted key if passphrase is non empty
            pkEncrypted = credentialJson["encrypted_private_key"]

        print(datetime.now().strftime("\n%H:%M:%S "),
              "=== generate sink connector rest reqeuest from {} ===".format(rest_template_path))
        if not os.path.exists(rest_generate_path):
            os.makedirs(rest_generate_path)
        snowflake_connector_name = fileName.split(".")[0] + nameSalt
        snowflake_topic_name = snowflake_connector_name

        print(datetime.now().strftime("\n%H:%M:%S "),
              "=== Connector Config JSON: {}, Connector Name: {} ===".format(fileName, snowflake_connector_name))
        with open("{}/{}".format(rest_template_path, fileName), 'r') as f:
            fileContent = f.read()
            # Template has passphrase, use the encrypted version of P8 Key
            if fileContent.find("snowflake.private.key.passphrase") != -1:
                pk = pkEncrypted

            fileContent = fileContent \
                .replace("SNOWFLAKE_PRIVATE_KEY", pk) \
                .replace("SNOWFLAKE_HOST", testHost) \
                .replace("SNOWFLAKE_USER", testUser) \
                .replace("SNOWFLAKE_DATABASE", testDatabase) \
                .replace("SNOWFLAKE_SCHEMA", testSchema) \
                .replace("CONFLUENT_SCHEMA_REGISTRY", self.schemaRegistryAddress) \
                .replace("SNOWFLAKE_TEST_TOPIC", snowflake_topic_name) \
                .replace("SNOWFLAKE_CONNECTOR_NAME", snowflake_connector_name) \
                .replace("SNOWFLAKE_ROLE", testRole)
            with open("{}/{}".format(rest_generate_path, fileName), 'w') as fw:
                fw.write(fileContent)

        MAX_RETRY = 3
        retry = 0
        delete_url = "http://{}/connectors/{}".format(self.kafkaConnectAddress, snowflake_connector_name)
        post_url = "http://{}/connectors".format(self.kafkaConnectAddress)
        while retry < MAX_RETRY:
            try:
                print("Delete request:{0}".format(delete_url))
                code = requests.delete(delete_url, timeout=10).status_code
                print("Delete request returned:{0}".format(code))
                if code == 404 or code == 200 or code == 201:
                    break
            except BaseException as e:
                print('An exception occurred: {}'.format(e))
                pass
            print(datetime.now().strftime("\n%H:%M:%S "),
                  "=== sleep for 30 secs to wait for kafka connect to accept connection ===")
            sleep(30)
            retry += 1
        if retry == MAX_RETRY:
            print("Kafka Delete request not successful:{0}".format(delete_url))

        print("Post HTTP request to Create Connector:{0}".format(post_url))
        r = requests.post(post_url, json=json.loads(fileContent), headers=self.httpHeader)
        print("Response Content Json " + json.dumps(r.json()))
        print(datetime.now().strftime("%H:%M:%S "), r.status_code)
        getConnectorResponse = requests.get(post_url)
        print("Get Connectors status:{0}, response:{1}".format(getConnectorResponse.status_code,
                                                               getConnectorResponse.content))


def runDeliveryGuaranteeTests(driver, testSet, nameSalt):
    if driver.snowflakeCloudPlatform == 'GCS' or driver.snowflakeCloudPlatform is None:
        print("Not running Delivery Guarantee tests in GCS due to flakiness")
        return

    print("Begin Delivery Guarantee tests in:" + str(driver.snowflakeCloudPlatform))
    # atleast once and exactly once testing
    testExactlyOnceSemantics = TestExactlyOnceSemantic(driver, nameSalt)
    testAtleastOnceSemantics = TestAtLeastOnceSemantic(driver, nameSalt)
    testExactlyOnceSemanticsTimeBuffer = TestExactlyOnceSemanticTimeBased(driver, nameSalt)

    print(datetime.now().strftime("\n%H:%M:%S "), "=== Exactly Once Test ===")
    testSuitList4 = [testExactlyOnceSemantics]

    testCleanEnableList4 = [True]
    testSuitEnableList4 = []
    if testSet == "confluent":
        testSuitEnableList4 = [True]
    elif testSet == "apache":
        testSuitEnableList4 = [True]
    elif testSet != "clean":
        errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

    execution(testSet, testSuitList4, testCleanEnableList4, testSuitEnableList4, driver, nameSalt)

    print(datetime.now().strftime("\n%H:%M:%S "), "=== At least Once Test ===")
    testSuitList5 = [testAtleastOnceSemantics]

    testCleanEnableList5 = [True]
    testSuitEnableList5 = []
    if testSet == "confluent":
        testSuitEnableList5 = [True]
    elif testSet == "apache":
        testSuitEnableList5 = [True]
    elif testSet != "clean":
        errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

    execution(testSet, testSuitList5, testCleanEnableList5, testSuitEnableList5, driver, nameSalt)

    print(datetime.now().strftime("\n%H:%M:%S "), "=== Exactly Once with Time Threshold ===")
    testSuitList6 = [testExactlyOnceSemanticsTimeBuffer]

    testCleanEnableList6 = [True]
    testSuitEnableList6 = []
    if testSet == "confluent":
        testSuitEnableList6 = [True]
    elif testSet == "apache":
        testSuitEnableList6 = [True]
    elif testSet != "clean":
        errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

    execution(testSet, testSuitList6, testCleanEnableList6, testSuitEnableList6, driver, nameSalt)


# These tests run from StressTest.yml file and not ran while running End-To-End Tests
def runStressTests(driver, testSet, nameSalt):
    from test_suit.test_pressure import TestPressure
    from test_suit.test_pressure_restart import TestPressureRestart

    testPressure = TestPressure(driver, nameSalt)

    # This test is more of a chaos test where we pause, delete, restart connectors to verify behavior.
    testPressureRestart = TestPressureRestart(driver, nameSalt)

    ############################ Stress Tests Round 1 ############################
    # TestPressure and TestPressureRestart will only run when Running StressTests
    print(datetime.now().strftime("\n%H:%M:%S "), "=== Stress Tests Round 1 ===")
    testSuitList = [testPressureRestart]

    testCleanEnableList = [True]
    testSuitEnableList = []
    if testSet == "confluent":
        testSuitEnableList = [True]
    elif testSet == "apache":
        testSuitEnableList = [True]
    elif testSet != "clean":
        errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

    execution(testSet, testSuitList, testCleanEnableList, testSuitEnableList, driver, nameSalt, round=1)
    ############################ Stress Tests Round 1 ############################

    ############################ Stress Tests Round 2 ############################
    print(datetime.now().strftime("\n%H:%M:%S "), "=== Stress Tests Round 2 ===")
    testSuitList = [testPressure]

    testCleanEnableList = [True]
    testSuitEnableList = []
    if testSet == "confluent":
        testSuitEnableList = [True]
    elif testSet == "apache":
        testSuitEnableList = [True]
    elif testSet != "clean":
        errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

    execution(testSet, testSuitList, testCleanEnableList, testSuitEnableList, driver, nameSalt, round=1)
    ############################ Stress Tests Round 2 ############################


def runTestSet(driver, testSet, nameSalt, enable_stress_test):
    if enable_stress_test:
        runStressTests(driver, testSet, nameSalt)
    else:
        from test_suit.test_string_json import TestStringJson
        from test_suit.test_string_json_proxy import TestStringJsonProxy
        from test_suit.test_json_json import TestJsonJson
        from test_suit.test_string_avro import TestStringAvro
        from test_suit.test_avro_avro import TestAvroAvro
        from test_suit.test_string_avrosr import TestStringAvrosr
        from test_suit.test_avrosr_avrosr import TestAvrosrAvrosr

        from test_suit.test_native_string_avrosr import TestNativeStringAvrosr
        from test_suit.test_native_string_json_without_schema import TestNativeStringJsonWithoutSchema
        from test_suit.test_native_complex_smt import TestNativeComplexSmt

        from test_suit.test_native_string_protobuf import TestNativeStringProtobuf
        from test_suit.test_confluent_protobuf_protobuf import TestConfluentProtobufProtobuf

        from test_suit.test_snowpipe_streaming_string_json import TestSnowpipeStreamingStringJson
        from test_suit.test_snowpipe_streaming_string_avro_sr import TestSnowpipeStreamingStringAvroSR

        from test_suit.test_multiple_topic_to_one_table_snowpipe_streaming import \
            TestMultipleTopicToOneTableSnowpipeStreaming
        from test_suit.test_multiple_topic_to_one_table_snowpipe import TestMultipleTopicToOneTableSnowpipe

        from test_suit.test_schema_mapping import TestSchemaMapping

        from test_suit.test_auto_table_creation import TestAutoTableCreation
        from test_suit.test_auto_table_creation_topic2table import TestAutoTableCreationTopic2Table

        from test_suit.test_schema_evolution_json import TestSchemaEvolutionJson
        from test_suit.test_schema_evolution_avro_sr import TestSchemaEvolutionAvroSR

        from test_suit.test_schema_evolution_w_auto_table_creation_json import \
            TestSchemaEvolutionWithAutoTableCreationJson
        from test_suit.test_schema_evolution_w_auto_table_creation_avro_sr import \
            TestSchemaEvolutionWithAutoTableCreationAvroSR

        from test_suit.test_schema_evolution_nonnullable_json import TestSchemaEvolutionNonNullableJson

        from test_suit.test_schema_not_supported_converter import TestSchemaNotSupportedConverter

        testStringJson = TestStringJson(driver, nameSalt)
        testJsonJson = TestJsonJson(driver, nameSalt)
        testStringAvro = TestStringAvro(driver, nameSalt)
        testAvroAvro = TestAvroAvro(driver, nameSalt)
        testStringAvrosr = TestStringAvrosr(driver, nameSalt)
        testAvrosrAvrosr = TestAvrosrAvrosr(driver, nameSalt)

        testNativeStringAvrosr = TestNativeStringAvrosr(driver, nameSalt)
        testNativeStringJsonWithoutSchema = TestNativeStringJsonWithoutSchema(driver, nameSalt)
        testNativeComplexSmt = TestNativeComplexSmt(driver, nameSalt)

        testNativeStringProtobuf = TestNativeStringProtobuf(driver, nameSalt)
        testConfluentProtobufProtobuf = TestConfluentProtobufProtobuf(driver, nameSalt)

        testStringJsonProxy = TestStringJsonProxy(driver, nameSalt)

        # Run this test on both confluent and apache kafka
        testSnowpipeStreamingStringJson = TestSnowpipeStreamingStringJson(driver, nameSalt)

        # will run this only in confluent cloud since, since in apache kafka e2e tests, we don't start schema registry
        testSnowpipeStreamingStringAvro = TestSnowpipeStreamingStringAvroSR(driver, nameSalt)

        testMultipleTopicToOneTableSnowpipeStreaming = TestMultipleTopicToOneTableSnowpipeStreaming(driver, nameSalt)
        testMultipleTopicToOneTableSnowpipe = TestMultipleTopicToOneTableSnowpipe(driver, nameSalt)

        testSchemaMapping = TestSchemaMapping(driver, nameSalt)

        testAutoTableCreation = TestAutoTableCreation(driver, nameSalt, schemaRegistryAddress, testSet)
        testAutoTableCreationTopic2Table = TestAutoTableCreationTopic2Table(driver, nameSalt, schemaRegistryAddress,
                                                                            testSet)

        testSchemaEvolutionJson = TestSchemaEvolutionJson(driver, nameSalt)
        testSchemaEvolutionAvroSR = TestSchemaEvolutionAvroSR(driver, nameSalt)

        testSchemaEvolutionWithAutoTableCreationJson = TestSchemaEvolutionWithAutoTableCreationJson(driver, nameSalt)
        testSchemaEvolutionWithAutoTableCreationAvroSR = TestSchemaEvolutionWithAutoTableCreationAvroSR(driver,
                                                                                                        nameSalt)

        testSchemaEvolutionNonNullableJson = TestSchemaEvolutionNonNullableJson(driver, nameSalt)

        testSchemaNotSupportedConverter = TestSchemaNotSupportedConverter(driver, nameSalt)

        ############################ round 1 ############################
        print(datetime.now().strftime("\n%H:%M:%S "), "=== Round 1 ===")
        testSuitList1 = [
            testStringJson, testJsonJson, testStringAvro, testAvroAvro, testStringAvrosr,
            testAvrosrAvrosr, testNativeStringAvrosr, testNativeStringJsonWithoutSchema,
            testNativeComplexSmt, testNativeStringProtobuf, testConfluentProtobufProtobuf,
            testSnowpipeStreamingStringJson, testSnowpipeStreamingStringAvro,
            testMultipleTopicToOneTableSnowpipeStreaming, testMultipleTopicToOneTableSnowpipe,
            testSchemaMapping,
            testAutoTableCreation, testAutoTableCreationTopic2Table,
            testSchemaEvolutionJson, testSchemaEvolutionAvroSR,
            testSchemaEvolutionWithAutoTableCreationJson, testSchemaEvolutionWithAutoTableCreationAvroSR,
            testSchemaEvolutionNonNullableJson,
            testSchemaNotSupportedConverter
        ]

        # Adding StringJsonProxy test at the end
        testCleanEnableList1 = [
            True, True, True, True, True, True, True, True, True, True, True,
            True, True,
            True, True,
            True,
            True, True,
            True, True,
            True, True,
            True, True
        ]
        testSuitEnableList1 = []
        if testSet == "confluent":
            testSuitEnableList1 = [
                True, True, True, True, True, True, True, True, True, True, False,
                True, True,
                True, True,
                True,
                True, True,
                True, True,
                True, True,
                True, True
            ]
        elif testSet == "apache":
            testSuitEnableList1 = [
                True, True, True, True, False, False, False, True, True, True, False,
                True, False,
                True, True,
                True,
                False, False,
                True, False,
                True, False,
                True, True
            ]
        elif testSet != "clean":
            errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

        execution(testSet, testSuitList1, testCleanEnableList1, testSuitEnableList1, driver, nameSalt)
        ############################ round 1 ############################

        print("Enable Delivery Guarantee tests:" + str(driver.enableDeliveryGuaranteeTests))
        if driver.enableDeliveryGuaranteeTests:
            # At least once and exactly once guarantee tests runs only in AWS and AZURE
            runDeliveryGuaranteeTests(driver, testSet, nameSalt)

        ############################ Always run Proxy tests in the end ############################

        ############################ Proxy End To End Test ############################
        print(datetime.now().strftime("\n%H:%M:%S "), "=== Last Round: Proxy E2E Test ===")
        print("Proxy Test should be the last test, since it modifies the JVM values")
        testSuitList4 = [testStringJsonProxy]

        # Should we invoke clean before and after the test
        testCleanEnableList4 = [True]

        # should we enable this? Set to false to disable
        testSuitEnableList4 = []
        if testSet == "confluent":
            testSuitEnableList4 = [True]
        elif testSet == "apache":
            testSuitEnableList4 = [True]
        elif testSet != "clean":
            errorExit("Unknown testSet option {}, please input confluent, apache or clean".format(testSet))

        execution(testSet, testSuitList4, testCleanEnableList4, testSuitEnableList4, driver, nameSalt)
        ############################ Proxy End To End Test End ############################


def execution(testSet, testSuitList, testCleanEnableList, testSuitEnableList, driver, nameSalt, round=1):
    if testSet == "clean":
        for i, test in enumerate(testSuitList):
            if testCleanEnableList[i]:
                test.clean()
        print(datetime.now().strftime("\n%H:%M:%S "), "=== All clean done ===")
    else:
        try:
            for i, test in enumerate(testSuitList):
                if testSuitEnableList[i]:
                    driver.createConnector(test.getConfigFileName(), nameSalt)

            driver.startConnectorWaitTime()

            for r in range(round):
                print(datetime.now().strftime("\n%H:%M:%S "), "=== round {} ===".format(r))
                for i, test in enumerate(testSuitList):
                    if testSuitEnableList[i]:
                        print(datetime.now().strftime("\n%H:%M:%S "),
                              "=== Sending " + test.__class__.__name__ + " data ===")
                        test.send()
                        print(datetime.now().strftime("%H:%M:%S "), "=== Done " + test.__class__.__name__ + " ===",
                              flush=True)

                driver.verifyWaitTime()

                for i, test in enumerate(testSuitList):
                    if testSuitEnableList[i]:
                        print(datetime.now().strftime("\n%H:%M:%S "), "=== Verify " + test.__class__.__name__ + " ===")
                        driver.verifyWithRetry(test.verify, r)
                        print(datetime.now().strftime("%H:%M:%S "), "=== Passed " + test.__class__.__name__ + " ===",
                              flush=True)

            print(datetime.now().strftime("\n%H:%M:%S "), "=== All test passed ===")
        except Exception as e:
            print(datetime.now().strftime("%H:%M:%S "), e)
            traceback.print_tb(e.__traceback__)
            print(datetime.now().strftime("%H:%M:%S "), "Error: ", sys.exc_info()[0])
            exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 9:
        errorExit(
            """\n=== Usage: ./ingest.py <kafka address> <schema registry address> <kafka connect address>
             <test set> <test version> <name salt> <pressure> <enableSSL>===""")

    kafkaAddress = sys.argv[1]
    global schemaRegistryAddress
    schemaRegistryAddress = sys.argv[2]
    kafkaConnectAddress = sys.argv[3]
    testSet = sys.argv[4]
    testVersion = sys.argv[5]
    nameSalt = sys.argv[6]
    pressure = (sys.argv[7] == 'true')
    enableSSL = (sys.argv[8] == 'true')

    if "SNOWFLAKE_CREDENTIAL_FILE" not in os.environ:
        errorExit(
            "\n=== Require environment variable SNOWFLAKE_CREDENTIAL_FILE but it's not set.  Aborting. ===")

    credentialPath = os.environ['SNOWFLAKE_CREDENTIAL_FILE']

    if not os.path.isfile(credentialPath):
        errorExit("\n=== Provided SNOWFLAKE_CREDENTIAL_FILE {} does not exist.  Aborting. ===".format(
            credentialPath))

    # This will either be AWS, AZURE or GCS
    snowflakeCloudPlatform = None

    # If it is not set, we will not run delivery guarantee tests
    enableDeliveryGuaranteeTests = False
    if "SF_CLOUD_PLATFORM" in os.environ:
        snowflakeCloudPlatform = os.environ['SF_CLOUD_PLATFORM']

    if "ENABLE_DELIVERY_GUARANTEE_TESTS" in os.environ:
        enableDeliveryGuaranteeTests = (os.environ['ENABLE_DELIVERY_GUARANTEE_TESTS'] == 'True')

    kafkaTest = KafkaTest(kafkaAddress,
                          schemaRegistryAddress,
                          kafkaConnectAddress,
                          credentialPath,
                          testVersion,
                          enableSSL,
                          snowflakeCloudPlatform,
                          enableDeliveryGuaranteeTests)

    runTestSet(kafkaTest, testSet, nameSalt, pressure)
