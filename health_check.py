#!/usr/bin/env python3
"""
OCI Load Balancer Health MCP Server (health_check.py)

- OCI Load Balancer / Network Load Balancer의 Health 정보를 조회하는 전용 MCP 서버
  * get_load_balancer_health
  * get_load_balancer_backendset_health
  * get_load_balancer_health_checker
  * get_network_load_balancer_health
  * get_network_load_balancer_backendset_health
  * get_network_load_balancer_health_checker
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict

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
log = logging.getLogger("oci-lb-health-mcp")


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

    def get_lb_client(self) -> oci.load_balancer.LoadBalancerClient:
        return oci.load_balancer.LoadBalancerClient(self.config, **self._common_kwargs())

    def get_nlb_client(self) -> oci.network_load_balancer.NetworkLoadBalancerClient:
        return oci.network_load_balancer.NetworkLoadBalancerClient(
            self.config, **self._common_kwargs()
        )


oci_manager = OCIManager()


# ---------- MCP 서버 정의 ----------

mcp = FastMCP("oci-lb-health")


# ---- 1. Classic Load Balancer ----

@mcp.tool()
def list_load_balancers(compartment_id: str) -> Dict[str, Any]:
    """
    Classic Load Balancer 목록 조회

    Args:
        compartment_id: 조회할 compartment OCID

    Returns:
        LB 이름, OCID, 상태 등을 포함하는 목록
    """
    try:
        lb = oci_manager.get_lb_client()
        resp = lb.list_load_balancers(compartment_id=compartment_id)
        items = [to_dict(x) for x in resp.data]

        return {
            "compartment_id": compartment_id,
            "count": len(items),
            "load_balancers": items,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("list_load_balancers ServiceError")
        return {
            "compartment_id": compartment_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("list_load_balancers failed")
        return {
            "compartment_id": compartment_id,
            "error": str(e),
        }

@mcp.tool()
def get_load_balancer_health(load_balancer_id: str) -> Dict[str, Any]:
    """
    Classic Load Balancer 전체 Health 조회

    Args:
        load_balancer_id: LB OCID (oci lb load-balancer-health get 과 동일 대상)

    Returns:
        overall_status, backend_sets 등의 정보를 포함한 dict
    """
    try:
        lb = oci_manager.get_lb_client()
        resp = lb.get_load_balancer_health(load_balancer_id=load_balancer_id)
        return {
            "load_balancer_id": load_balancer_id,
            "health": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_load_balancer_health ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_load_balancer_health failed")
        return {
            "load_balancer_id": load_balancer_id,
            "error": str(e),
        }


@mcp.tool()
def get_load_balancer_backendset_health(
    load_balancer_id: str,
    backend_set_name: str,
) -> Dict[str, Any]:
    """
    Classic Load Balancer 특정 Backend Set Health 조회

    Args:
        load_balancer_id: LB OCID
        backend_set_name: 백엔드셋 이름 (콘솔/CLI에 보이는 이름과 동일)

    Returns:
        백엔드셋 Health + 각 Backend Health 를 포함한 dict
    """
    try:
        lb = oci_manager.get_lb_client()
        resp = lb.get_backend_set_health(
            load_balancer_id=load_balancer_id,
            backend_set_name=backend_set_name,
        )
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_set_health": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_load_balancer_backendset_health ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_load_balancer_backendset_health failed")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }


@mcp.tool()
def get_load_balancer_health_checker(
    load_balancer_id: str,
    backend_set_name: str,
) -> Dict[str, Any]:
    """
    Classic Load Balancer의 Health Check Policy 설정 조회

    Args:
        load_balancer_id: LB OCID
        backend_set_name: Health Check가 설정된 Backend Set 이름

    Returns:
        intervalMs, timeoutMs, retries, urlPath 등 Health Check 설정 정보
    """
    try:
        lb = oci_manager.get_lb_client()
        resp = lb.get_health_checker(
            load_balancer_id=load_balancer_id,
            backend_set_name=backend_set_name,
        )
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "health_checker": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_load_balancer_health_checker ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_load_balancer_health_checker failed")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }

@mcp.tool()
def add_load_balancer_backend(
    load_balancer_id: str,
    backend_set_name: str,
    ip_address: str,
    port: int,
    weight: int = 1,
    backup: bool = False,
    drain: bool = False,
    offline: bool = False,
) -> Dict[str, Any]:
    """
    Classic Load Balancer 백엔드 서버 추가

    Args:
        load_balancer_id: LB OCID
        backend_set_name: 백엔드셋 이름
        ip_address: 백엔드 서버 IP (프라이빗 IP)
        port: 백엔드 포트 (예: 80, 8080 등)
        weight: 가중치 (기본 1)
        backup: 백업 백엔드로 설정 여부
        drain: drain 모드 여부
        offline: offline 모드 여부

    Returns:
        생성된 Backend 정보 또는 에러 정보
    """
    try:
        lb = oci_manager.get_lb_client()
        details = oci.load_balancer.models.CreateBackendDetails(
            ip_address=ip_address,
            port=port,
            weight=weight,
            backup=backup,
            drain=drain,
            offline=offline,
        )

        resp = lb.create_backend(
            load_balancer_id=load_balancer_id,
            backend_set_name=backend_set_name,
            create_backend_details=details,
        )

        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend": to_dict(resp.data),
        }

    except oci.exceptions.ServiceError as e:
        log.exception("add_load_balancer_backend ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("add_load_balancer_backend failed")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }

@mcp.tool()
def remove_load_balancer_backend(
    load_balancer_id: str,
    backend_set_name: str,
    ip_address: str,
    port: int,
) -> Dict[str, Any]:
    """
    Classic Load Balancer 백엔드 서버 삭제

    Args:
        load_balancer_id: LB OCID
        backend_set_name: 백엔드셋 이름
        ip_address: 백엔드 서버 IP
        port: 백엔드 포트

    Returns:
        삭제 결과 또는 에러 정보
    """
    backend_name = f"{ip_address}:{port}"

    try:
        lb = oci_manager.get_lb_client()
        lb.delete_backend(
            load_balancer_id=load_balancer_id,
            backend_set_name=backend_set_name,
            backend_name=backend_name,
        )

        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "result": "deleted",
        }

    except oci.exceptions.ServiceError as e:
        log.exception("remove_load_balancer_backend ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("remove_load_balancer_backend failed")
        return {
            "load_balancer_id": load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "error": str(e),
        }

@mcp.tool()
def list_lb_backend_sets(load_balancer_id: str) -> Dict[str, Any]:
    """Classic Load Balancer의 BackendSet 이름 목록 조회"""
    try:
        lb = oci_manager.get_lb_client()
        resp = lb.get_load_balancer(load_balancer_id=load_balancer_id)
        data = to_dict(resp.data)

        backend_sets = list((data.get("backend_sets") or {}).keys())

        return {
            "load_balancer_id": load_balancer_id,
            "backend_sets": backend_sets,
            "count": len(backend_sets),
        }

    except Exception as e:
        return {
            "load_balancer_id": load_balancer_id,
            "error": str(e),
        }

@mcp.tool()
def delete_load_balancer(
    load_balancer_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    Classic Load Balancer 삭제 (dry-run / confirm 지원)

    Args:
        load_balancer_id: 삭제 대상 LB OCID
        dry_run: True 이면 실제 삭제하지 않고, 삭제 시 영향을 받는 리소스만 보여줍니다.
        confirm: 실제 삭제를 실행할지 여부. (dry_run=False AND confirm=True 일 때만 삭제)

    Returns:
        - dry_run 시: 삭제 계획(리스너/백엔드셋/백엔드 요약)
        - 실제 삭제 시: 삭제 요청 결과 + work request id
    """
    lb = oci_manager.get_lb_client()

    try:
        # 1) 현재 LB 상세 정보 및 구성 가져오기
        lb_resp = lb.get_load_balancer(load_balancer_id=load_balancer_id)
        lb_info = to_dict(lb_resp.data)

        # 리스너 / 백엔드셋 / 백엔드 요약
        listeners = lb_info.get("listeners", {})
        backend_sets = lb_info.get("backend_sets", {})

        # backend_sets 는 dict[backend_set_name] = { "backends": [...] } 구조
        backend_summary = []
        for bs_name, bs in backend_sets.items():
            backends = bs.get("backends") or []
            backend_summary.append(
                {
                    "backend_set_name": bs_name,
                    "backend_count": len(backends),
                    "backends": backends,
                }
            )

        plan = {
            "load_balancer_id": load_balancer_id,
            "display_name": lb_info.get("display_name"),
            "lifecycle_state": lb_info.get("lifecycle_state"),
            "shape_name": lb_info.get("shape_name"),
            "ip_addresses": lb_info.get("ip_addresses"),
            "listeners": list(listeners.keys()),
            "backend_sets": backend_summary,
        }

        # 2) dry-run 이면 여기까지만 리턴
        if dry_run or not confirm:
            return {
                "action": "dry-run" if dry_run else "not-confirmed",
                "message": (
                    "아래 Load Balancer 및 관련 리소스가 삭제 대상입니다. "
                    "실제로 삭제하려면 dry_run=False, confirm=True 로 호출하세요."
                ),
                "delete_plan": plan,
            }

        # 3) 실제 삭제 실행
        resp = lb.delete_load_balancer(load_balancer_id=load_balancer_id)
        work_request_id = resp.headers.get("opc-work-request-id")

        return {
            "action": "delete",
            "load_balancer_id": load_balancer_id,
            "result": "delete requested",
            "opc_work_request_id": work_request_id,
            "previous_state": plan,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("delete_load_balancer ServiceError")
        return {
            "load_balancer_id": load_balancer_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("delete_load_balancer failed")
        return {
            "load_balancer_id": load_balancer_id,
            "error": str(e),
        }

# ---- 2. Network Load Balancer ----

@mcp.tool()
def list_network_load_balancers(compartment_id: str) -> Dict[str, Any]:
    """
    Network Load Balancer 목록 조회

    Args:
        compartment_id: 조회할 compartment OCID

    Returns:
        NLB 이름, OCID, 상태 등을 포함한 목록
    """
    try:
        nlb = oci_manager.get_nlb_client()
        resp = nlb.list_network_load_balancers(compartment_id=compartment_id)

        # ✅ resp.data는 NetworkLoadBalancerCollection 이라서 .items 를 써야 함
        items = [to_dict(x) for x in (resp.data.items or [])]

        return {
            "compartment_id": compartment_id,
            "count": len(items),
            "network_load_balancers": items,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("list_network_load_balancers ServiceError")
        return {
            "compartment_id": compartment_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("list_network_load_balancers failed")
        return {
            "compartment_id": compartment_id,
            "error": str(e),
        }



@mcp.tool()
def get_network_load_balancer_health(
    network_load_balancer_id: str,
) -> Dict[str, Any]:
    """
    Network Load Balancer 전체 Health 조회

    Args:
        network_load_balancer_id: NLB OCID

    Returns:
        overall status, backend_sets health 등의 정보를 포함한 dict
    """
    try:
        nlb = oci_manager.get_nlb_client()
        resp = nlb.get_network_load_balancer_health(
            network_load_balancer_id=network_load_balancer_id
        )
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "health": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_network_load_balancer_health ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_network_load_balancer_health failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "error": str(e),
        }


@mcp.tool()
def get_network_load_balancer_backendset_health(
    network_load_balancer_id: str,
    backend_set_name: str,
) -> Dict[str, Any]:
    """
    Network Load Balancer 특정 Backend Set Health 조회
    """
    try:
        nlb = oci_manager.get_nlb_client()
        resp = nlb.get_backend_set_health(
            network_load_balancer_id=network_load_balancer_id,
            backend_set_name=backend_set_name,
        )
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_set_health": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_network_load_balancer_backendset_health ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_network_load_balancer_backendset_health failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }


@mcp.tool()
def get_network_load_balancer_health_checker(
    network_load_balancer_id: str,
    backend_set_name: str,
) -> Dict[str, Any]:
    """
    Network Load Balancer Health Check Policy 설정 조회
    """
    try:
        nlb = oci_manager.get_nlb_client()
        resp = nlb.get_health_checker(
            network_load_balancer_id=network_load_balancer_id,
            backend_set_name=backend_set_name,
        )
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "health_checker": to_dict(resp.data),
        }
    except oci.exceptions.ServiceError as e:
        log.exception("get_network_load_balancer_health_checker ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("get_network_load_balancer_health_checker failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }

@mcp.tool()
def add_network_load_balancer_backend(
    network_load_balancer_id: str,
    backend_set_name: str,
    ip_address: str,
    port: int,
    weight: int = 1,
    is_backup: bool = False,
    is_drain: bool = False,
    is_offline: bool = False,
) -> Dict[str, Any]:
    """
    Network Load Balancer 백엔드 서버 추가

    Args:
        network_load_balancer_id: NLB OCID
        backend_set_name: 백엔드셋 이름
        ip_address: 백엔드 서버 IP (프라이빗 IP)
        port: 백엔드 포트
        weight: 가중치
        is_backup: 백업 백엔드 여부
        is_drain: drain 모드 여부
        is_offline: offline 모드 여부

    Returns:
        생성된 Backend 정보 또는 에러 정보
    """
    try:
        nlb = oci_manager.get_nlb_client()

        # SDK 버전에 따라 CreateBackendDetails 필드명이 조금 다를 수 있음
        details = oci.network_load_balancer.models.CreateBackendDetails(
            ip_address=ip_address,
            port=port,
            weight=weight,
            is_backup=is_backup,
            is_drain=is_drain,
            is_offline=is_offline,
        )

        resp = nlb.create_backend(
            network_load_balancer_id=network_load_balancer_id,
            backend_set_name=backend_set_name,
            create_backend_details=details,
        )

        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend": to_dict(resp.data),
        }

    except oci.exceptions.ServiceError as e:
        log.exception("add_network_load_balancer_backend ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("add_network_load_balancer_backend failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "error": str(e),
        }

@mcp.tool()
def list_nlb_backend_sets(network_load_balancer_id: str) -> Dict[str, Any]:
    """Network Load Balancer의 BackendSet 이름 목록 조회"""
    try:
        nlb = oci_manager.get_nlb_client()
        resp = nlb.list_backend_sets(
            network_load_balancer_id=network_load_balancer_id
        )

        # ✅ resp.data 는 BackendSetCollection → .items 로 접근
        backend_sets = [bs.name for bs in (resp.data.items or [])]

        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_sets": backend_sets,
            "count": len(backend_sets),
        }

    except Exception as e:
        log.exception("list_nlb_backend_sets failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "error": str(e),
        }

@mcp.tool()
def remove_network_load_balancer_backend(
    network_load_balancer_id: str,
    backend_set_name: str,
    ip_address: str,
    port: int,
) -> Dict[str, Any]:
    """
    Network Load Balancer 백엔드 서버 삭제

    Args:
        network_load_balancer_id: NLB OCID
        backend_set_name: 백엔드셋 이름
        ip_address: 백엔드 서버 IP
        port: 백엔드 포트

    Returns:
        삭제 결과 또는 에러 정보
    """
    backend_name = f"{ip_address}:{port}"

    try:
        nlb = oci_manager.get_nlb_client()
        nlb.delete_backend(
            network_load_balancer_id=network_load_balancer_id,
            backend_set_name=backend_set_name,
            backend_name=backend_name,
        )

        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "result": "deleted",
        }

    except oci.exceptions.ServiceError as e:
        log.exception("remove_network_load_balancer_backend ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("remove_network_load_balancer_backend failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "backend_set_name": backend_set_name,
            "backend_name": backend_name,
            "error": str(e),
        }

@mcp.tool()
def delete_network_load_balancer(
    network_load_balancer_id: str,
    dry_run: bool = True,
    confirm: bool = False,
) -> Dict[str, Any]:
    """
    Network Load Balancer 삭제 (dry-run / confirm 지원)

    Args:
        network_load_balancer_id: 삭제 대상 NLB OCID
        dry_run: True 이면 실제 삭제하지 않고, 삭제 시 영향을 받는 리소스만 보여줍니다.
        confirm: 실제 삭제를 실행할지 여부. (dry_run=False AND confirm=True 일 때만 삭제)

    Returns:
        - dry-run 시: 삭제 계획(리스너/백엔드셋/백엔드 요약)
        - 실제 삭제 시: 삭제 요청 결과 + work request id
    """
    nlb = oci_manager.get_nlb_client()

    try:
        # 1) 현재 NLB 상세 정보
        nlb_resp = nlb.get_network_load_balancer(
            network_load_balancer_id=network_load_balancer_id
        )
        nlb_info = to_dict(nlb_resp.data)

        # 2) Backend Set 목록 (⚠️ Collection → .data.items)
        bs_resp = nlb.list_backend_sets(
            network_load_balancer_id=network_load_balancer_id
        )
        backend_sets = [to_dict(x) for x in (bs_resp.data.items or [])]

        backend_summary = []
        for bs in backend_sets:
            bs_name = bs.get("name")
            # 각 backend set 에 대한 backend 목록 (역시 Collection → .data.items)
            try:
                be_resp = nlb.list_backends(
                    network_load_balancer_id=network_load_balancer_id,
                    backend_set_name=bs_name,
                )
                backends = [to_dict(x) for x in (be_resp.data.items or [])]
            except Exception:
                backends = []

            backend_summary.append(
                {
                    "backend_set_name": bs_name,
                    "policy": bs.get("policy"),
                    "is_preserve_source": bs.get("is_preserve_source"),
                    "backend_count": len(backends),
                    "backends": backends,
                }
            )

        # 3) Listener 목록 (이것도 Collection → .data.items)
        listeners_resp = nlb.list_listeners(
            network_load_balancer_id=network_load_balancer_id
        )
        listeners = [to_dict(x) for x in (listeners_resp.data.items or [])]

        plan = {
            "network_load_balancer_id": network_load_balancer_id,
            "display_name": nlb_info.get("display_name"),
            "lifecycle_state": nlb_info.get("lifecycle_state"),
            "is_private": nlb_info.get("is_private"),
            "ip_addresses": nlb_info.get("ip_addresses"),
            "listeners": listeners,
            "backend_sets": backend_summary,
        }

        # 4) dry-run 이면 여기까지만 리턴
        if dry_run or not confirm:
            return {
                "action": "dry-run" if dry_run else "not-confirmed",
                "message": (
                    "아래 Network Load Balancer 및 관련 리소스가 삭제 대상입니다. "
                    "실제로 삭제하려면 dry_run=False, confirm=True 로 호출하세요."
                ),
                "delete_plan": plan,
            }

        # 5) 실제 삭제 실행
        resp = nlb.delete_network_load_balancer(
            network_load_balancer_id=network_load_balancer_id
        )
        work_request_id = resp.headers.get("opc-work-request-id")

        return {
            "action": "delete",
            "network_load_balancer_id": network_load_balancer_id,
            "result": "delete requested",
            "opc_work_request_id": work_request_id,
            "previous_state": plan,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("delete_network_load_balancer ServiceError")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "error": f"ServiceError {e.status} {e.code}: {e.message}",
        }
    except Exception as e:
        log.exception("delete_network_load_balancer failed")
        return {
            "network_load_balancer_id": network_load_balancer_id,
            "error": str(e),
        }



# ---------- 엔트리포인트 ----------

if __name__ == "__main__":
    mcp.run()
