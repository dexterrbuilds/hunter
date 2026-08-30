"""Low-latency feed aggregation for Hunter's maximum-performance profile."""

from monitoring.performance.aggregator import EarliestEventAggregator
from monitoring.performance.config import (
    InfrastructureConfig,
    InfrastructureProfile,
    infrastructure_config_from_dict,
)
from monitoring.performance.fast_path import FastPathConfidence, assess_fast_path
from monitoring.performance.models import DetectionIdentity, DetectionObservation

__all__ = [
    "DetectionIdentity",
    "DetectionObservation",
    "EarliestEventAggregator",
    "FastPathConfidence",
    "InfrastructureConfig",
    "InfrastructureProfile",
    "assess_fast_path",
    "infrastructure_config_from_dict",
]
