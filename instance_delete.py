#!/usr/bin/env python3
"""
OCI Instance Manager MCP Server (instance_manager.py)

Tools:
  * list_instances
  * get_instance
  * terminate_instance (dry-run + confirm)
  * terminate_instances_bulk (dry-run + confirm)

Notes:
- 실제 삭제는 dry_run=False AND confirm=True 일 때만 수행합니다.
- instance OCID에서 region(ap-seoul-1 등)을 자동 추출해서 해당 리전으로 ComputeClient를 생성합니다.
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

import oci
from oci.util import to_dict


# ---------- 공통 설정 ----------

load_dotenv()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("oci-instance-mcp")

mcp = FastMCP("oci-instance-manager")


# ---------- OCI 헬퍼 ----------

class OCIManager:
    def __init__(self) -> None:
        self.signer = None
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        cfg_file = os.getenv("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
        profile = os.getenv("OCI_CONFIG_PROFILE", "DEFAULT")

        if os.path.exists(cfg_file):
            log.info(f"Using OCI config file: {cfg_file} [{profile}]")
            return oci.config.from_file(cfg_file, profile_name=profile)

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

        # Resource Principal (OCI 인스턴스에서 실행 시)
        try:
            self.signer = oci.auth.signers.get_resource_principals_signer()
            region = os.getenv("OCI_REGION", "ap-seoul-1")
            log.info("Using resource principals signer")
            return {"region": region, "tenancy": os.getenv("OCI_TENANCY_OCID", "")}
        except Exception:
            raise RuntimeError("No OCI credentials found. Configure OCI credentials first.")

    def _kwargs(self) -> Dict[str, Any]:
        return {"signer": self.signer} if self.signer else {}

    def _config_with_region(self, region: Optional[str]) -> Dict[str, Any]:
        if not region:
            return self.config
        cfg = dict(self.config)
        cfg["region"] = region
        return cfg

    def compute(self, region: Optional[str] = None) -> oci.core.ComputeClient:
        return oci.core.ComputeClient(self._config_with_region(region), **self._kwargs())

    def vcn(self, region: Optional[str] = None) -> oci.core.VirtualNetworkClient:
        return oci.core.VirtualNetworkClient(self._config_with_region(region), **self._kwargs())


oci_manager = OCIManager()


# ---------- 유틸 ----------

def _infer_region_from_instance_ocid(instance_id: str) -> Optional[str]:
    """
    instance OCID 예:
      ocid1.instance.oc1.ap-seoul-1.<random>
    여기서 'ap-seoul-1' 추출
    """
    try:
        parts = instance_id.split(".")
        # ["ocid1", "instance", "oc1", "ap-seoul-1", "..."]
        if len(parts) >= 4 and parts[0] == "ocid1" and parts[2].startswith("oc"):
            return parts[3]
    except Exception:
        pass
    return None


def _compute_client_for_instance(instance_id: str, region: Optional[str]) -> Tuple[oci.core.ComputeClient, str]:
    eff_region = region or _infer_region_from_instance_ocid(instance_id) or oci_manager.config.get("region")
    if not eff_region:
        raise RuntimeError("Region not provided and could not be inferred from instance OCID/config.")
    return oci_manager.compute(region=eff_region), eff_region


def _summarize_instance(compute: oci.core.ComputeClient, instance_id: str) -> Dict[str, Any]:
    inst = compute.get_instance(instance_id=instance_id).data
    d = to_dict(inst)

    # 사람이 보기 쉽게 핵심만 얇게 요약 + 원본도 일부 포함
    return {
        "id": d.get("id"),
        "display_name": d.get("display_name"),
        "lifecycle_state": d.get("lifecycle_state"),
        "compartment_id": d.get("compartment_id"),
        "availability_domain": d.get("availability_domain"),
        "shape": d.get("shape"),
        "time_created": d.get("time_created"),
        "freeform_tags": d.get("freeform_tags"),
        "defined_tags": d.get("defined_tags"),
        "raw": d,  # 필요하면 전체도 확인 가능
    }


# ---------- Tools ----------

@mcp.tool()
def list_instances(
    compartment_id: str,
    lifecycle_state: Optional[str] = None,
    region: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """
    Compartment 내 인스턴스 목록 조회

    Args:
      compartment_id: 컴파트먼트 OCID
      lifecycle_state: (옵션) RUNNING/STOPPED 등
      region: (옵션) 리전 지정. 미지정 시 config region 사용.
      limit: 최대 몇 개까지 가져올지(기본 200)

    Returns:
      instances: [{id, display_name, lifecycle_state, time_created, ...}]
    """
    try:
        eff_region = region or oci_manager.config.get("region")
        if not eff_region:
            return {"error": "region not provided and not found in config"}

        compute = oci_manager.compute(region=eff_region)

        # pagination 처리
        results: List[Dict[str, Any]] = []
        page = None
        while True:
            resp = compute.list_instances(
                compartment_id=compartment_id,
                lifecycle_state=lifecycle_state,
                page=page,
            )
            for it in (resp.data or []):
                d = to_dict(it)
                results.append(
                    {
                        "id": d.get("id"),
                        "display_name": d.get("display_name"),
                        "lifecycle_state": d.get("lifecycle_state"),
                        "availability_domain": d.get("availability_domain"),
                        "shape": d.get("shape"),
                        "time_created": d.get("time_created"),
                        "compartment_id": d.get("compartment_id"),
                    }
                )
                if len(results) >= limit:
                    return {
                        "region": eff_region,
                        "compartment_id": compartment_id,
                        "lifecycle_state": lifecycle_state,
                        "count": len(results),
                        "note": f"limit({limit}) reached",
                        "instances": results,
                    }

            page = resp.headers.get("opc-next-page")
            if not page:
                break

        return {
            "region": eff_region,
            "compartment_id": compartment_id,
            "lifecycle_state": lifecycle_state,
            "count": len(results),
            "instances": results,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("list_instances ServiceError")
        return {"error": f"ServiceError {e.status} {e.code}: {e.message}"}
    except Exception as e:
        log.exception("list_instances failed")
        return {"error": str(e)}


@mcp.tool()
def get_instance(
    instance_id: str,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """단일 인스턴스 상세 조회"""
    try:
        compute, eff_region = _compute_client_for_instance(instance_id, region)
        info = _summarize_instance(compute, instance_id)
        return {"region": eff_region, "instance": info}
    except oci.exceptions.ServiceError as e:
        log.exception("get_instance ServiceError")
        return {"instance_id": instance_id, "error": f"ServiceError {e.status} {e.code}: {e.message}"}
    except Exception as e:
        log.exception("get_instance failed")
        return {"instance_id": instance_id, "error": str(e)}


@mcp.tool()
def terminate_instance(
    instance_id: str,
    preserve_boot_volume: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    인스턴스 종료(삭제) = Compute terminate_instance

    안전장치:
      - dry_run=True 이면 절대 삭제 안 함
      - 실제 삭제는 dry_run=False AND confirm=True 인 경우에만 수행

    Args:
      instance_id: 인스턴스 OCID
      preserve_boot_volume: True면 Boot Volume 유지(기본 True)
      dry_run: 기본 True
      confirm: 기본 False
      region: (옵션) 미지정 시 instance OCID에서 추출 or config region

    Returns:
      dry-run: 삭제 계획(현재 상태/이름/태그 등)
      delete: 삭제 요청 결과(opc-work-request-id 있을 수 있음)
    """
    try:
        compute, eff_region = _compute_client_for_instance(instance_id, region)

        # 삭제 전 상태 요약
        current = _summarize_instance(compute, instance_id)

        plan = {
            "instance_id": instance_id,
            "region": eff_region,
            "preserve_boot_volume": preserve_boot_volume,
            "current": {
                "display_name": current.get("display_name"),
                "lifecycle_state": current.get("lifecycle_state"),
                "compartment_id": current.get("compartment_id"),
                "availability_domain": current.get("availability_domain"),
                "shape": current.get("shape"),
                "time_created": current.get("time_created"),
            },
            "safety": {
                "dry_run": dry_run,
                "confirm": confirm,
                "will_execute": (not dry_run) and confirm,
            },
        }

        if dry_run or not confirm:
            return {
                "action": "dry-run" if dry_run else "not-confirmed",
                "message": "실제 삭제하려면 dry_run=False, confirm=True 로 호출하세요.",
                "plan": plan,
            }

        # 실제 삭제
        resp = compute.terminate_instance(
            instance_id=instance_id,
            preserve_boot_volume=preserve_boot_volume,
        )
        wr_id = resp.headers.get("opc-work-request-id")

        return {
            "action": "terminate",
            "result": "terminate requested",
            "plan": plan,
            "opc_work_request_id": wr_id,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("terminate_instance ServiceError")
        return {"instance_id": instance_id, "error": f"ServiceError {e.status} {e.code}: {e.message}"}
    except Exception as e:
        log.exception("terminate_instance failed")
        return {"instance_id": instance_id, "error": str(e)}


@mcp.tool()
def terminate_instances_bulk(
    instance_ids: List[str],
    preserve_boot_volume: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    인스턴스 여러 개 종료(삭제) - dry-run/confirm 동일 정책

    주의:
      - instance마다 region이 다를 수 있어서, 기본은 OCID에서 region을 추출해서 처리함.
      - region 파라미터를 주면 전체에 공통 적용(같은 리전 인스턴스만 넣는 것을 권장)
    """
    results = []
    for iid in instance_ids:
        results.append(
            terminate_instance(
                instance_id=iid,
                preserve_boot_volume=preserve_boot_volume,
                dry_run=dry_run,
                confirm=confirm,
                region=region,
            )
        )
    return {
        "count": len(instance_ids),
        "dry_run": dry_run,
        "confirm": confirm,
        "preserve_boot_volume": preserve_boot_volume,
        "results": results,
    }


# ---------- 엔트리포인트 ----------

if __name__ == "__main__":
    mcp.run()
