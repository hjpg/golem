import datetime
import enum
import hashlib
import inspect
import json
import pickle
import struct
import sys
import time
from typing import Any, Dict, Optional

from eth_utils import decode_hex, encode_hex
from ethereum.utils import denoms
import golem_messages
from golem_messages import cryptography
from golem_messages import datastructures as msg_dt
from golem_messages import message
from golem_messages.datastructures import p2p as dt_p2p, masking
from peewee import (
    BlobField,
    BooleanField,
    CharField,
    CompositeKey,
    DateTimeField,
    Field,
    FloatField,
    ForeignKeyField,
    IntegerField,
    Model,
    SmallIntegerField,
    TextField,
)
import semantic_version

from golem.core import common
from golem.core.common import datetime_to_timestamp, default_now
from golem.core.simpleserializer import DictSerializable
from golem.core.variables import MESSAGE_QUEUE_MAX_AGE
from golem.database import GolemSqliteDatabase
from golem.ranking.helper.trust_const import NEUTRAL_TRUST
from golem.ranking import ProviderEfficacy
from golem.task import taskstate


# TODO: migrate to golem.database. issue #2415
db = GolemSqliteDatabase(None, threadlocals=True,
                         pragmas=(
                             ('foreign_keys', True),
                             ('busy_timeout', 1000),
                             ('journal_mode', 'WAL')))


def default_dict():
    return {}


default_dict.__self__ = dict  # type: ignore


def default_list():
    return []


default_list.__self__ = list  # type: ignore


class UTCDateTimeField(DateTimeField):
    formats = DateTimeField.formats + [
        '%Y-%m-%d %H:%M:%S+00:00',
        '%Y-%m-%d %H:%M:%S.%f+00:00',
    ]

    def python_value(self, value):
        value = super().python_value(value)
        if value is None:
            return None
        return value.replace(tzinfo=datetime.timezone.utc)


class BaseModel(Model):
    class Meta:
        # WARNING: Meta won't be inherited by subclasses
        #          due too meta class hack in peewee...
        # SEE:
        #  https://github.com/coleifer/peewee/blob/2.10.2/peewee.py#L4831-L4836
        # In other words - you have to define Meta in every
        # class that inherits from peewee.Model
        database = db

    created_date = UTCDateTimeField(default=default_now)
    modified_date = UTCDateTimeField(default=default_now)

    def refresh(self):
        """
        https://github.com/coleifer/peewee/issues/686#issuecomment-130548126
        :return: Refreshed version of the object retrieved from db
        """
        return type(self).get(self._pk_expr())


class GenericKeyValue(BaseModel):
    key = CharField(primary_key=True)
    value = CharField(null=True)

    class Meta:
        database = db


##################
# PAYMENT MODELS #
##################


class RawCharField(CharField):
    """ Char field without auto utf-8 encoding."""

    def db_value(self, value):
        return encode_hex(value)[2:]

    def python_value(self, value):
        return decode_hex(value)


class HexIntegerField(CharField):
    """ Standard Integer field is limited to 2^63-1. This field extends the
        range by storing the numbers as hex-encoded char strings.
    """

    def db_value(self, value):
        if not isinstance(value, int):
            raise TypeError("Value {} is not an integer".format(value))
        return format(value, 'x')

    def python_value(self, value):
        if value is not None:
            return int(value, 16)
        return None


class FixedLengthHexField(CharField):
    EXPECTED_LENGTH: int = 0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, max_length=self.EXPECTED_LENGTH, **kwargs)

    def db_value(self, value: str):
        value = super().db_value(value)
        if value is None:
            return None
        current_len = len(value)
        if len(value) != self.EXPECTED_LENGTH:
            raise ValueError(
                "Value {value} has length of {has}"
                " not {should} characters".format(
                    value=value,
                    has=current_len,
                    should=self.EXPECTED_LENGTH,
                ),
            )
        return value


class BlockchainTransactionField(FixedLengthHexField):
    EXPECTED_LENGTH = 66


class EnumFieldBase:
    enum_type = None

    def db_value(self, value):
        if isinstance(value, self.enum_type):
            return value.value  # Get the base-type value of an enum.

        value = self.coerce(value)  # noqa pylint:disable=no-member
        enum_vals = [e.value for e in self.enum_type]
        if value not in enum_vals:
            raise TypeError(
                "Expected {} type or one of {}".format(
                    self.enum_type.__name__, enum_vals))

        return value

    def python_value(self, value):
        # pylint: disable=not-callable
        return self.enum_type(value)


class ProviderEfficacyField(CharField):

    def db_value(self, value):
        if not isinstance(value, ProviderEfficacy):
            raise TypeError("Value {} is not an instance of ProviderEfficacy"
                            .format(value))
        return value.serialize()

    def python_value(self, value):
        if value is not None:
            return ProviderEfficacy.deserialize(value)
        return None


class EnumField(EnumFieldBase, IntegerField):
    """ Database field that maps enum type to integer."""

    def __init__(self, *args, enum_type=None, **kwargs):
        super(EnumField, self).__init__(*args, **kwargs)
        self.enum_type = enum_type


class StringEnumField(EnumFieldBase, CharField):
    """ Database field that maps enum types to strings."""

    def __init__(self, *args, max_length=255, enum_type=None, **kwargs):
        # Because of peewee_migrate limitations
        # we have to provide default enum_type
        # (peewee_migrate only understands max_length in CharField
        #  subclasses)
        # noqa SEE: https://github.com/klen/peewee_migrate/blob/c55cb8c3664c3d59e6df3da7126b3ddae3fb7b39/peewee_migrate/auto.py#L41  pylint: disable=line-too-long
        super().__init__(*args, max_length=max_length, **kwargs)
        self.enum_type = enum_type


class JsonField(TextField):
    """ Database field that stores a Python value in JSON format. """

    def db_value(self, value):
        return json.dumps(value)

    def python_value(self, value):
        return json.loads(value)


class DictSerializableJSONField(TextField):
    """ Database field that stores a Node in JSON format. """
    objtype: DictSerializable

    def db_value(self, value: Optional[DictSerializable]) -> str:
        if value is None:
            return json.dumps(None)
        return json.dumps(value.to_dict())

    def python_value(self, value: str) -> DictSerializable:
        if issubclass(self.objtype, msg_dt.Container):  # type: ignore
            # pylint: disable=not-callable
            return self.objtype(**json.loads(value))  # type: ignore
        return self.objtype.from_dict(json.loads(value))


class NodeField(DictSerializableJSONField):
    """ Database field that stores a Node in JSON format. """
    objtype = dt_p2p.Node


class VersionField(CharField):
    """Semantic version field"""

    def db_value(self, value):
        if not isinstance(value, semantic_version.Version):
            raise TypeError(f"Value {value} is not a semantic version")
        return str(value)

    def python_value(self, value):
        if value is not None:
            return semantic_version.Version(value)
        return None


class WalletOperation(BaseModel):
    class STATUS(msg_dt.StringEnum):
        awaiting = enum.auto()
        sent = enum.auto()
        confirmed = enum.auto()
        overdue = enum.auto()
        failed = enum.auto()
        arbitraged_by_concent = enum.auto()

    class DIRECTION(msg_dt.StringEnum):
        incoming = enum.auto()
        outgoing = enum.auto()

    class TYPE(msg_dt.StringEnum):
        transfer = enum.auto()  # topup & withdraw
        deposit_transfer = enum.auto()  # deposit topup & withdraw
        task_payment = enum.auto()
        deposit_payment = enum.auto()  # forced payments

    class CURRENCY(msg_dt.StringEnum):
        ETH = enum.auto()
        GNT = enum.auto()

    tx_hash = BlockchainTransactionField(null=True)
    direction = StringEnumField(enum_type=DIRECTION)
    operation_type = StringEnumField(enum_type=TYPE)
    status = StringEnumField(enum_type=STATUS)
    sender_address = CharField()
    recipient_address = CharField()
    amount = HexIntegerField()
    currency = StringEnumField(enum_type=CURRENCY)
    gas_cost = HexIntegerField()

    class Meta:
        database = db

    def __repr__(self):
        return (
            f"WalletOperation. tx_hash={self.tx_hash},"
            f" direction={self.direction}, type={self.operation_type},"
            f" amount={self.amount/denoms.ether}{self.currency}"
        )

    @classmethod
    def deposit_transfers(cls):
        return cls.select() \
            .where(
                WalletOperation.operation_type
                == WalletOperation.TYPE.deposit_transfer)

    @classmethod
    def transfers(cls):
        return cls.select() \
            .where(
                WalletOperation.operation_type
                == WalletOperation.TYPE.transfer)

    @classmethod
    def unconfirmed_payments(cls):
        return cls.select() \
            .where(
                cls.status.not_in([
                    cls.STATUS.confirmed,
                    cls.STATUS.failed,
                    cls.STATUS.arbitraged_by_concent,
                ]),
                cls.tx_hash.is_null(False),
                cls.direction ==
                cls.DIRECTION.outgoing,
                cls.operation_type.in_([
                    cls.TYPE.transfer,
                    cls.TYPE.deposit_transfer,
                ]))

    def on_confirmed(self, gas_cost: int):
        if self.operation_type not in (
                self.TYPE.transfer,
                self.TYPE.deposit_transfer
        ):
            return
        if self.direction != self.DIRECTION.outgoing:
            return
        self.gas_cost = gas_cost  # type: ignore
        self.status = self.STATUS.confirmed  # type: ignore

    def on_failed(self, gas_cost: int):
        self.gas_cost = gas_cost  # type: ignore
        self.status = self.STATUS.failed  # type: ignore


class TaskPayment(BaseModel):
    wallet_operation = ForeignKeyField(WalletOperation, unique=True)
    node = CharField()
    task = CharField()
    subtask = CharField()
    expected_amount = HexIntegerField()
    accepted_ts = IntegerField(null=True)
    settled_ts = IntegerField(null=True)  # set if settled by the Concent
    charged_from_deposit = BooleanField(null=True)

    class Meta:
        database = db

    def __repr__(self):
        return (
            f"TaskPayment. accepted_ts={self.accepted_ts},"
            f" task={self.task}, subtask={self.subtask},"
            f" node={self.node}, wo={self.wallet_operation}"
        )

    @classmethod
    def incomes(cls):
        return cls.select() \
            .join(WalletOperation) \
            .where(
                WalletOperation.operation_type
                == WalletOperation.TYPE.task_payment,
                WalletOperation.direction
                == WalletOperation.DIRECTION.incoming)

    @classmethod
    def payments(cls):
        return cls.select() \
            .join(WalletOperation) \
            .where(
                WalletOperation.operation_type
                == WalletOperation.TYPE.task_payment,
                WalletOperation.direction
                == WalletOperation.DIRECTION.outgoing)

    @property
    def missing_amount(self):
        # pylint: disable=no-member
        return self.expected_amount - self.wallet_operation.amount

##################
# RANKING MODELS #
##################


def provider_efficacy_producer():
    def producer():
        return ProviderEfficacy(0., 0., 0., 0.)
    # peewee-migrate expects a '__self__' attribute
    producer.__self__ = ProviderEfficacy
    return producer


class LocalRank(BaseModel):
    """ Represent nodes experience with other nodes, number of positive and
    negative interactions.
    """
    node_id = CharField(unique=True)
    positive_computed = FloatField(default=0.0)
    negative_computed = FloatField(default=0.0)
    wrong_computed = FloatField(default=0.0)
    positive_requested = FloatField(default=0.0)
    negative_requested = FloatField(default=0.0)
    positive_payment = FloatField(default=0.0)
    negative_payment = FloatField(default=0.0)
    positive_resource = FloatField(default=0.0)
    negative_resource = FloatField(default=0.0)
    requestor_efficiency = FloatField(default=None, null=True)
    requestor_assigned_sum = HexIntegerField(default=0)
    requestor_paid_sum = HexIntegerField(default=0)
    provider_efficiency = FloatField(default=1.0)
    provider_efficacy = ProviderEfficacyField(
        default=provider_efficacy_producer())

    class Meta:
        database = db


class GlobalRank(BaseModel):
    """ Represents global ranking vector estimation
    """
    node_id = CharField(unique=True)
    requesting_trust_value = FloatField(default=NEUTRAL_TRUST)
    computing_trust_value = FloatField(default=NEUTRAL_TRUST)
    gossip_weight_computing = FloatField(default=0.0)
    gossip_weight_requesting = FloatField(default=0.0)

    class Meta:
        database = db


class NeighbourLocRank(BaseModel):
    """ Represents neighbour trust level for other nodes
    """
    node_id = CharField()
    about_node_id = CharField()
    requesting_trust_value = FloatField(default=NEUTRAL_TRUST)
    computing_trust_value = FloatField(default=NEUTRAL_TRUST)

    class Meta:
        database = db
        primary_key = CompositeKey('node_id', 'about_node_id')


##################
# NETWORK MODELS #
##################


class KnownHosts(BaseModel):
    ip_address = CharField()
    port = IntegerField()
    last_connected = DateTimeField(default=datetime.datetime.now)
    is_seed = BooleanField(default=False)
    metadata = JsonField(default='{}')

    class Meta:
        database = db
        indexes = (
            (('ip_address', 'port'), True),  # unique index
        )


##################
# ACCOUNT MODELS #
##################


class Account(BaseModel):
    node_id = CharField(unique=True)

    class Meta:
        database = db


class Stats(BaseModel):
    name = CharField()
    value = CharField()

    class Meta:
        database = db


class HardwarePreset(BaseModel):
    name = CharField(null=False, index=True, unique=True)

    cpu_cores = SmallIntegerField(null=False)
    memory = IntegerField(null=False)
    disk = IntegerField(null=False)

    def to_dict(self):
        return {
            'name': str(self.name),
            'cpu_cores': self.cpu_cores,
            'memory': self.memory,
            'disk': self.disk
        }

    def apply(self, dictionary: dict) -> None:
        self.cpu_cores = dictionary['cpu_cores']
        self.memory = dictionary['memory']
        self.disk = dictionary['disk']

    class Meta:
        database = db


##############
# APP MODELS #
##############


class TaskPreset(BaseModel):
    name = CharField(null=False)
    task_type = CharField(null=False, index=True)
    data = JsonField(null=False)

    class Meta:
        database = db
        primary_key = CompositeKey('task_type', 'name')


class Performance(BaseModel):
    """ Keeps information about benchmark performance """
    DEFAULT_CPU_USAGE = 1_000_000_000   # 1 second in nanoseconds

    environment_id = CharField(null=False, index=True, unique=True)
    value = FloatField(default=0.0)
    min_accepted_step = FloatField(default=300.0)
    cpu_usage = IntegerField(default=DEFAULT_CPU_USAGE)

    class Meta:
        database = db

    @staticmethod
    def update_or_create(env_id: str, performance: float, cpu_usage: int):
        try:
            stored = Performance.get(Performance.environment_id == env_id)
            stored.value = performance
            stored.cpu_usage = cpu_usage
            stored.save()
        except Performance.DoesNotExist:
            Performance(
                environment_id=env_id,
                value=performance,
                cpu_usage=cpu_usage
            ).save()


class AppBenchmark(BaseModel):

    hash = CharField(null=False, index=True, unique=True)
    score = FloatField()
    cpu_usage = IntegerField(default=1)  # total CPU usage in nanoseconds

    class Meta:
        database = db


class DockerWhitelist(BaseModel):
    repository = CharField(primary_key=True)

    class Meta:
        database = db


class ACLAllowedNodes(BaseModel):
    node_name = CharField(null=True)
    node_id = CharField(null=False, index=True, unique=True)

    def to_dict(self):
        return {
            'node_name': self.node_name,
            'node_id': self.node_id
        }

    class Meta:
        database = db


class ACLDeniedNodes(BaseModel):
    node_name = CharField(null=True)
    node_id = CharField(null=False, index=True, unique=True)

    def to_dict(self):
        return {
            'node_name': self.node_name,
            'node_id': self.node_id
        }

    class Meta:
        database = db

##################
# MESSAGE MODELS #
##################


class Actor(enum.Enum):
    Concent = "concent"
    Requestor = "requestor"
    Provider = "provider"


class ActorField(StringEnumField):
    """ Database field that stores Actor objects as strings. """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, enum_type=Actor, **kwargs)


class NetworkMessage(BaseModel):
    local_role = ActorField(null=False)
    remote_role = ActorField(null=False)

    # The node we are exchanging messages with. Can be a receiver or a sender,
    # which is determined by local_role, remote_role and msg_cls.
    node = CharField(null=False)
    task = CharField(null=True, index=True)
    subtask = CharField(null=True, index=True)

    msg_date = DateTimeField(null=False)
    msg_cls = CharField(null=False)
    msg_data = BlobField(null=False)

    class Meta:
        database = db

    def as_message(self) -> message.base.Message:
        msg = pickle.loads(self.msg_data)
        return msg


def default_msg_deadline() -> datetime.datetime:
    return default_now() + MESSAGE_QUEUE_MAX_AGE


class QueuedMessage(BaseModel):
    node = CharField(null=False, index=True)
    msg_version = VersionField(null=False)
    msg_cls = CharField(null=False)
    msg_data = BlobField(null=False)
    deadline = UTCDateTimeField(
        null=False,
        default=default_msg_deadline()
    )

    class Meta:
        database = db

    @classmethod
    def from_message(
            cls,
            node_id: str,
            msg: message.base.Message,
            deadline: Optional[datetime.datetime] = None):
        instance = cls()
        instance.node = node_id
        instance.msg_cls = '.'.join(
            [msg.__class__.__module__, msg.__class__.__qualname__, ],
        )
        instance.msg_version = semantic_version.Version(
            golem_messages.__version__,
        )
        instance.msg_data = golem_messages.dump(msg, None, None)
        instance.deadline = deadline or default_msg_deadline()  # type: ignore
        return instance

    def as_message(self) -> message.base.Message:
        message.base.verify_version(str(self.msg_version))
        msg = golem_messages.load(
            self.msg_data,
            None,
            None,
            check_time=False,
        )
        msg.header = msg_dt.MessageHeader(
            msg.header[0],
            int(time.time()),
            False,
        )
        msg.sig = None
        return msg

    def __str__(self):
        node = self.node or ''
        return (
            f"{ self.__class__.__name__ }"
            f" node={common.short_node_id(node)}"
            f", version={self.msg_version}"
            f", class={self.msg_cls}"
        )


class CachedNode(BaseModel):
    node = CharField(null=False, index=True, unique=True)
    node_field = NodeField(null=False)

    class Meta:
        database = db

    def __str__(self):
        # pylint: disable=no-member
        node_name = self.node_field.node_name if self.node_field else ''
        node_id = self.node or ''
        return (
            f"{common.node_info_str(node_name, node_id)}"
        )

    def __repr__(self):
        return (
            f"<{self.__class__.__module__}.{self.__class__.__qualname__}:"
            f" {self}>"
        )

####################
# REQUESTOR MODELS #
####################


class RequestedTask(BaseModel):
    task_id = CharField(primary_key=True)
    app_id = CharField(null=False)
    name = CharField(null=True)
    status = StringEnumField(enum_type=taskstate.TaskStatus, null=False)

    env_id = CharField(null=True)
    prerequisites = JsonField(null=False, default=default_dict())

    task_timeout = IntegerField(null=False)  # seconds
    subtask_timeout = IntegerField(null=False)  # seconds
    start_time = UTCDateTimeField(null=True)
    end_time = UTCDateTimeField(null=True)

    max_price_per_hour = HexIntegerField(null=False)
    max_subtasks = IntegerField(null=False)
    min_memory = IntegerField(null=False, default=0)

    concent_enabled = BooleanField(null=False, default=False)
    mask = BlobField(null=False, default=masking.Mask().to_bytes())
    output_directory = CharField(null=False)
    app_params = JsonField(null=False, default=default_dict())

    class Meta:
        database = db

    @property
    def deadline(self) -> Optional[datetime.datetime]:
        if self.start_time is None:
            return None
        assert isinstance(self.start_time, datetime.datetime)
        return self.start_time + \
            datetime.timedelta(seconds=self.task_timeout)

    @property
    def elapsed_seconds(self) -> Optional[int]:
        if self.start_time is None:
            return None
        assert isinstance(self.start_time, datetime.datetime)
        return (default_now() - self.start_time).total_seconds()

    def estimated_fee(self) -> float:
        return self.max_price_per_hour * (
            self.subtask_timeout
            * self.max_subtasks
            / 60  # subtask timeout is seconds, convert to hour
        )


class ComputingNode(BaseModel):
    node_id = CharField(primary_key=True)
    name = CharField(null=False)

    class Meta:
        database = db

    def __repr__(self):
        return (
            f"ComputingNode <"
            f"node_id={self.node_id}, "
            f"name={self.name}"
            f">"
        )


class RequestedSubtask(BaseModel):
    task = ForeignKeyField(RequestedTask, null=False, related_name='subtasks')
    subtask_id = CharField(null=False)
    status = StringEnumField(enum_type=taskstate.SubtaskStatus, null=False)

    payload = JsonField(null=False, default=default_dict())
    inputs = JsonField(null=False, default=default_list())
    start_time = UTCDateTimeField(null=True)
    price = HexIntegerField(null=True)
    computing_node = ForeignKeyField(
        ComputingNode, null=True, related_name='subtasks')

    @property
    def deadline(self) -> Optional[datetime.datetime]:
        if self.start_time is None:
            return None
        assert isinstance(self.start_time, datetime.datetime)
        return self.start_time + datetime.timedelta(
            milliseconds=self.task.subtask_timeout)  # pylint: disable=no-member

    def to_dict(self) -> Dict[str, Any]:
        return {
            'task_id': self.task.task_id,  # pylint: disable=no-member
            'subtask_id': self.subtask_id,
            'status': self.status.value,  # pylint: disable=no-member
            'time_started': datetime_to_timestamp(self.start_time),
            'node_id': self.computing_node.node_id,  # pylint: disable=no-member
            'node_name': self.computing_node.name,  # pylint: disable=no-member
        }

    class Meta:
        database = db
        primary_key = CompositeKey('task', 'subtask_id')


class QueuedVerification(BaseModel):
    task_id = CharField(null=False)
    subtask_id = CharField(null=False)
    priority = IntegerField(null=True, index=True)

    class Meta:
        database = db
        primary_key = CompositeKey('task_id', 'subtask_id')


class AppConfiguration(BaseModel):
    app_id = CharField(primary_key=True)
    enabled = BooleanField(null=False, default=False)

    class Meta:
        database = db


class EnvConfiguration(BaseModel):
    env_id = CharField(primary_key=True)
    enabled = BooleanField(null=False, default=False)

    class Meta:
        database = db


class UsageFactor(BaseModel):
    provider_node = ForeignKeyField(
        ComputingNode, null=False, unique=True, related_name='usage_factor')
    usage_factor = FloatField(default=1.0)

    def __repr__(self):
        return (
            f"UsageFactor <"
            f"provider={self.provider_node_id}, "
            f"usage_factor={self.usage_factor}"
            f">"
        )


class Broadcast(BaseModel):
    class TYPE(enum.IntEnum):
        Version = enum.auto()
    HEADER_FORMAT = '!HQ'
    HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)
    SIGNATURE_LENGTH = 65
    timestamp = IntegerField()
    broadcast_type = EnumField(enum_type=TYPE)
    signature = BlobField()
    data = BlobField()

    def __repr__(self):
        return (
            f"<{self.__class__.__qualname__}"
            f" {self.timestamp} {self.broadcast_type.name}>"
        )

    @classmethod
    def create_and_sign(cls, private_key, broadcast_type, data) -> 'Broadcast':
        bc = cls()
        bc.timestamp = int(time.time())
        bc.broadcast_type = broadcast_type
        bc.data = data
        bc.sign(private_key=private_key)
        bc.save(force_insert=True)
        return bc

    def process(self) -> bool:
        if Broadcast.select().where(
                Broadcast.broadcast_type == self.broadcast_type,
                Broadcast.timestamp == self.timestamp,
        ).exists():
            return False
        if self.broadcast_type is self.TYPE.Version:
            from golem.network.p2p.peersession import compare_version
            compare_version(self.data.decode('utf-8', 'replace'))
        self.save(force_insert=True)
        return True

    def header_to_bytes(self) -> bytes:
        return struct.pack(
            self.HEADER_FORMAT,
            self.broadcast_type,
            self.timestamp,
        )

    def header_from_bytes(self, b: bytes) -> None:
        try:
            broadcast_type, self.timestamp = struct.unpack(
                self.HEADER_FORMAT,
                b,
            )
            self.broadcast_type = self.TYPE(broadcast_type)  # type: ignore
        except (ValueError, struct.error):
            from golem.network import broadcast
            raise broadcast.BroadcastError('Invalid header')

    @classmethod
    def from_bytes(cls, b: bytes) -> 'Broadcast':
        # Remember to verify signature of this broadcast if it's been loaded
        # from untrusted source
        if len(b) < cls.HEADER_LENGTH + cls.SIGNATURE_LENGTH:
            from golem.network import broadcast
            raise broadcast.BroadcastError(
                'Invalid broadcast: too short'
                f' ({len(b)} < {cls.HEADER_LENGTH + cls.SIGNATURE_LENGTH})',
            )
        bc = cls()
        bc.header_from_bytes(b[:cls.HEADER_LENGTH])
        bc.signature = b[
            cls.HEADER_LENGTH:cls.HEADER_LENGTH+cls.SIGNATURE_LENGTH
        ]
        bc.data = b[cls.HEADER_LENGTH+cls.SIGNATURE_LENGTH:]
        return bc

    def to_bytes(self) -> bytes:
        return self.header_to_bytes() + self.signature + self.data

    def get_hash(self) -> bytes:
        sha = hashlib.sha1()
        sha.update(self.header_to_bytes())
        sha.update(self.data)
        return sha.digest()

    def sign(self, private_key: bytes) -> None:
        assert self.signature is None
        self.signature = cryptography.ecdsa_sign(
            privkey=private_key,
            msghash=self.get_hash(),
        )

    def verify_signature(self, public_key: bytes) -> None:
        cryptography.ecdsa_verify(
            pubkey=public_key,
            signature=self.signature,
            message=self.get_hash(),
        )


def collect_db_models(module: str = __name__):
    return inspect.getmembers(
        sys.modules[module],
        lambda cls: (
            inspect.isclass(cls) and
            issubclass(cls, BaseModel) and
            cls is not BaseModel
        )
    )


def collect_db_fields(module: str = __name__):
    return inspect.getmembers(
        sys.modules[module],
        lambda cls: (
            inspect.isclass(cls) and
            issubclass(cls, Field)
        )
    )


DB_FIELDS = [cls for _, cls in collect_db_fields()]
DB_MODELS = [cls for _, cls in collect_db_models()]
