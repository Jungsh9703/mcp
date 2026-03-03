#!/usr/bin/env python3
"""
OCI Metrics MCP Server

- OCI Monitoring을 통해 인스턴스 메트릭만 조회하는 전용 MCP 서버
  * get_instance_realtime_metrics
  * get_instance_metric_timeseries
"""

from __future__ import annotations

import os
import logging
from datetime import timezone, datetime, timedelta
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import oci

# ---------- 공통 설정 ----------

load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("oci-metrics-mcp")


# ---------- OCI 헬퍼 ----------

class OCIManager:
    """
    ~/.oci/config 또는 환경변수/인스턴스 프린시펄을 이용해
    OCI 클라이언트를 만드는 헬퍼
    """

    def __init__(self) -> None:
        self.signer = None
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        cfg_file = os.getenv("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
        profile = os.getenv("OCI_CONFIG_PROFILE", "DEFAULT")

        # 1) config 파일 우선
        if os.path.exists(cfg_file):
            log.info(f"Using OCI config file: {cfg_file} [{profile}]")
            return oci.config.from_file(cfg_file, profile_name=profile)

        # 2) 명시적 환경변수
        env_keys = (
            "OCI_USER_OCID",
            "OCI_FINGERPRINT",
            "OCI_TENANCY_OCID",
            "OCI_REGION",
            "OCI_KEY_FILE",
        )
        if all(os.getenv(k) for k in env_keys):
            log.info("Using explicit OCI env var configuration")
            return {
                "user": os.environ["OCI_USER_OCID"],
                "fingerprint": os.environ["OCI_FINGERPRINT"],
                "tenancy": os.environ["OCI_TENANCY_OCID"],
                "region": os.environ["OCI_REGION"],
                "key_file": os.environ["OCI_KEY_FILE"],
            }

        # 3) 인스턴스 프린시펄 (OCI VM 위에서 돌 때)
        try:
            self.signer = oci.auth.signers.get_resource_principals_signer()
            region = os.getenv("OCI_REGION", "ap-seoul-1")
            log.info("Using resource principals signer")
            return {"region": region, "tenancy": os.getenv("OCI_TENANCY_OCID", "")}
        except Exception:
            raise RuntimeError(
                "No OCI credentials found. Run `oci setup config` "
                "or set env vars (OCI_USER_OCID, OCI_FINGERPRINT, "
                "OCI_TENANCY_OCID, OCI_REGION, OCI_KEY_FILE)."
            )

    def _common_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {}
        if self.signer:
            kwargs["signer"] = self.signer
        return kwargs

    def get_monitoring_client(self) -> oci.monitoring.MonitoringClient:
        return oci.monitoring.MonitoringClient(self.config, **self._common_kwargs())

    def get_compute_client(self) -> oci.core.ComputeClient:
        return oci.core.ComputeClient(self.config, **self._common_kwargs())


oci_manager = OCIManager()


# ---------- 유틸: 인스턴스로부터 compartment 자동 조회 ----------

def _get_compartment_for_instance(instance_id: str) -> Optional[str]:
    """
    인스턴스 OCID로부터 compartment OCID를 조회.
    CLI에서 --compartment-id 넘기던 걸 여기서 자동으로 처리.
    """
    try:
        compute = oci_manager.get_compute_client()
        resp = compute.get_instance(instance_id)
        cid = resp.data.compartment_id
        log.debug(f"Instance {instance_id} compartment: {cid}")
        return cid
    except Exception as e:
        log.exception(f"Failed to get compartment for instance {instance_id}")
        return None


# ---------- 메트릭 헬퍼 (UTC만 사용) ----------

def _summarize_instance_metric(
    instance_id: str,
    metric_name: str,
    start_time: datetime,
    end_time: datetime,
    window: str = "1m",
    statistic: str = "mean",
) -> Dict[str, Any]:
    """
    OCI Monitoring에서 단일 메트릭을 조회하는 내부 헬퍼.
    - CLI에서 확인한 것과 동일한 쿼리 문법 사용:
      CpuUtilization[5m]{resourceId = "<인스턴스 OCID>"}.mean()
    - 모든 시각은 UTC 기준으로 처리/반환.
    """
    try:
        mon = oci_manager.get_monitoring_client()

        # 인스턴스에서 compartment 자동 조회
        compartment_id = _get_compartment_for_instance(instance_id)
        if not compartment_id:
            return {
                "metric_name": metric_name,
                "datapoints": [],
                "error": "인스턴스의 compartment-id 를 조회하지 못했습니다. OCI 권한 또는 instance_id 를 확인하세요.",
            }

        # UTC 보정
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        if end_time.tzinfo is None:
            end_time = end_time.replace(tzinfo=timezone.utc)

        # ✅ CLI에서 성공한 쿼리와 동일한 형태
        #   예: CpuUtilization[5m]{resourceId = "ocid1.instance..."}.mean()
        query = f'{metric_name}[{window}]{{resourceId = "{instance_id}"}}.{statistic}()'

        details = oci.monitoring.models.SummarizeMetricsDataDetails(
            namespace="oci_computeagent",
            query=query,
            start_time=start_time,
            end_time=end_time,
            resolution=window,
        )

        # Python SDK 시그니처: summarize_metrics_data(compartment_id, details, **kwargs)
        resp = mon.summarize_metrics_data(compartment_id, details)

        if not resp.data:
            return {
                "metric_name": metric_name,
                "datapoints": [],
                "note": "No metric data returned (agent 미설치이거나, 기간 내 데이터 없음일 수 있음).",
            }

        metric_data = resp.data[0]
        points = [
            {
                "timestamp_utc": dp.timestamp.isoformat(),
                "value": dp.value,
            }
            for dp in (metric_data.aggregated_datapoints or [])
        ]

        return {
            "metric_name": metric_name,
            "datapoints": points,
            "dimensions": metric_data.dimensions,
        }

    except oci.exceptions.ServiceError as e:
        log.exception(f"ServiceError while summarizing {metric_name} for {instance_id}")
        return {
            "metric_name": metric_name,
            "datapoints": [],
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception(f"Failed to summarize metric {metric_name} for {instance_id}")
        return {
            "metric_name": metric_name,
            "datapoints": [],
            "error": str(e),
        }


# ---------- MCP 서버 정의 ----------

mcp = FastMCP("oci-infra-monitoring")


@mcp.tool()
def get_instance_realtime_metrics(
    instance_id: str,
    window_minutes: int = 5,
) -> Dict[str, Any]:
    """
    최근 N분 동안 인스턴스의 CPU / Memory / Disk 사용률을 조회합니다.
    (모든 시각은 UTC 기준으로 반환)
    """
    end_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
    start_utc = end_utc - timedelta(minutes=window_minutes)

    cpu = _summarize_instance_metric(
        instance_id=instance_id,
        metric_name="CpuUtilization",
        start_time=start_utc,
        end_time=end_utc,
        window="1m",
        statistic="mean",
    )
    mem = _summarize_instance_metric(
        instance_id=instance_id,
        metric_name="MemoryUtilization",
        start_time=start_utc,
        end_time=end_utc,
        window="1m",
        statistic="mean",
    )
    disk = _summarize_instance_metric(
        instance_id=instance_id,
        metric_name="DiskUsedPercent",
        start_time=start_utc,
        end_time=end_utc,
        window="1m",
        statistic="mean",
    )

    return {
        "instance_id": instance_id,
        "window_minutes": window_minutes,
        "start_time_utc": start_utc.isoformat(),
        "end_time_utc": end_utc.isoformat(),
        "metrics": {
            "cpu": cpu,
            "memory": mem,
            "disk": disk,
        },
    }


@mcp.tool()
def get_instance_metric_timeseries(
    instance_id: str,
    metric_name: str,
    start_time_iso: str,
    end_time_iso: str,
    window: str = "5m",
    statistic: str = "mean",
) -> Dict[str, Any]:
    """
    지정한 기간 동안 인스턴스의 단일 메트릭 타임시리즈를 조회합니다.
    - 모든 입력/출력 시각은 UTC 기준으로 처리/반환
    """

    try:
        def _parse_iso(s: str) -> datetime:
            # 다양한 ISO8601 포맷을 UTC 기준으로 정규화
            # 예: '2025-12-10T15:00:00Z', '2025-12-10T15:00:00+09:00' 등
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo:
                return dt.astimezone(timezone.utc)
            return dt.replace(tzinfo=timezone.utc)

        start_utc = _parse_iso(start_time_iso)
        end_utc = _parse_iso(end_time_iso)

        data = _summarize_instance_metric(
            instance_id=instance_id,
            metric_name=metric_name,
            start_time=start_utc,
            end_time=end_utc,
            window=window,
            statistic=statistic,
        )

        return {
            "instance_id": instance_id,
            "metric_name": metric_name,
            "start_time_utc": start_utc.isoformat(),
            "end_time_utc": end_utc.isoformat(),
            "window": window,
            "statistic": statistic,
            "datapoints": data.get("datapoints", []),
            "dimensions": data.get("dimensions"),
            "note": data.get("note"),
            "error": data.get("error"),
        }

    except Exception as e:
        log.exception("get_instance_metric_timeseries failed")
        return {
            "instance_id": instance_id,
            "metric_name": metric_name,
            "error": str(e),
        }


# ---------- 엔트리포인트 ----------

if __name__ == "__main__":
    mcp.run()
