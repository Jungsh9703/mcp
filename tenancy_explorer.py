#!/usr/bin/env python3
"""
OCI Tenancy Explorer MCP Server (tenancy_explorer.py) - Minimal (Fixed)

Tools:
  * list_compartments_tree
      : tenancy의 compartment 목록(트리) 조회 + 이름 필터

  * list_existing_resources_in_compartment
      : 특정 compartment의 '실제 존재 리소스'만 조회
        - existing   : 검증(get) 성공
        - stale      : 404로 확인된 삭제 잔재
        - unverified : 타입 미지원/권한(403)/기타 사유로 검증 불가
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
log = logging.getLogger("oci-tenancy-explorer-min")


# ---------- OCI 헬퍼 ----------

class OCIManager:
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
            raise RuntimeError("No OCI credentials found. Configure OCI credentials first.")

    def _kwargs(self) -> Dict[str, Any]:
        return {"signer": self.signer} if self.signer else {}

    def _config_with_region(self, region: Optional[str]) -> Dict[str, Any]:
        if not region:
            return self.config
        cfg = dict(self.config)
        cfg["region"] = region
        return cfg

    def identity(self, region: Optional[str] = None) -> oci.identity.IdentityClient:
        return oci.identity.IdentityClient(self._config_with_region(region), **self._kwargs())

    def search(self, region: Optional[str] = None) -> oci.resource_search.ResourceSearchClient:
        return oci.resource_search.ResourceSearchClient(self._config_with_region(region), **self._kwargs())

    # 검증용 최소 클라이언트들
    def compute(self, region: Optional[str] = None) -> oci.core.ComputeClient:
        return oci.core.ComputeClient(self._config_with_region(region), **self._kwargs())

    def network(self, region: Optional[str] = None) -> oci.core.VirtualNetworkClient:
        return oci.core.VirtualNetworkClient(self._config_with_region(region), **self._kwargs())

    def lb(self, region: Optional[str] = None) -> oci.load_balancer.LoadBalancerClient:
        return oci.load_balancer.LoadBalancerClient(self._config_with_region(region), **self._kwargs())

    def nlb(self, region: Optional[str] = None) -> oci.network_load_balancer.NetworkLoadBalancerClient:
        return oci.network_load_balancer.NetworkLoadBalancerClient(self._config_with_region(region), **self._kwargs())

    def database(self, region: Optional[str] = None) -> oci.database.DatabaseClient:
        return oci.database.DatabaseClient(self._config_with_region(region), **self._kwargs())


oci_manager = OCIManager()
mcp = FastMCP("oci-tenancy-explorer")


# ---------- 유틸 ----------

def _paginate_search(
    client: oci.resource_search.ResourceSearchClient,
    search_details: oci.resource_search.models.StructuredSearchDetails,
    limit_total: int = 500,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    page = None

    while True:
        resp = client.search_resources(search_details, page=page)
        items = resp.data.items or []
        for it in items:
            results.append(to_dict(it))
            if len(results) >= limit_total:
                return results

        page = resp.headers.get("opc-next-page")
        if not page:
            return results


def _rtype_rid(item: Dict[str, Any]) -> Tuple[str, str]:
    """
    Resource Search 결과에서 resource-type / identifier 추출 + 정규화

    - resource-type은 콘솔/CLI에서 "Instance" 처럼 대문자로 오기도 함
      => 무조건 소문자로 normalize 해서 비교
    """
    rtype = item.get("resource-type") or item.get("resourceType") or ""
    rid = item.get("identifier") or item.get("id") or ""
    return str(rtype).strip().lower(), str(rid).strip()


def _item_region(item: Dict[str, Any]) -> Optional[str]:
    """
    Resource Search 결과에는 region이 포함될 수 있음.
    멀티리전 리소스 검증 시 이 값을 우선 사용.
    """
    r = item.get("region")
    return r if isinstance(r, str) and r else None


def verify_exists(item: Dict[str, Any], region: Optional[str] = None) -> Tuple[str, str]:
    """
    Return:
      ("exists", "")        : 실제 존재
      ("stale", reason)     : 404 로 확인(삭제된 잔재)
      ("unverified", reason): 검증 불가(타입/권한/기타)
    """
    rtype, rid = _rtype_rid(item)
    if not rtype or not rid:
        return ("unverified", "missing resource-type or identifier")

    # ✅ item에 region이 있으면 그걸 우선 사용
    eff_region = _item_region(item) or region

    try:
        if rtype == "instance":
            oci_manager.compute(region=eff_region).get_instance(rid)
            return ("exists", "")

        if rtype == "loadbalancer":
            oci_manager.lb(region=eff_region).get_load_balancer(rid)
            return ("exists", "")

        if rtype == "networkloadbalancer":
            oci_manager.nlb(region=eff_region).get_network_load_balancer(rid)
            return ("exists", "")

        if rtype == "vcn":
            oci_manager.network(region=eff_region).get_vcn(rid)
            return ("exists", "")

        if rtype == "subnet":
            oci_manager.network(region=eff_region).get_subnet(rid)
            return ("exists", "")

        # DB 관련: Search가 Summary 타입으로 주는 경우가 있어 폭넓게 처리
        if rtype in ("dbsystem", "dbsystemsummary"):
            oci_manager.database(region=eff_region).get_db_system(rid)
            return ("exists", "")

        if rtype in ("autonomousdatabase", "autonomousdatabasesummary"):
            oci_manager.database(region=eff_region).get_autonomous_database(rid)
            return ("exists", "")

        return ("unverified", f"no verifier for resource-type '{rtype}'")

    except oci.exceptions.ServiceError as e:
        if e.status == 404:
            return ("stale", f"404 {e.code}: {e.message}")
        if e.status == 403:
            return ("unverified", f"403 {e.code}: {e.message}")
        return ("unverified", f"{e.status} {e.code}: {e.message}")
    except Exception as e:
        return ("unverified", str(e))


# ---------- Tools ----------

@mcp.tool()
def list_compartments_tree(
    tenancy_id: Optional[str] = None,
    name_contains: Optional[str] = None,
    include_root: bool = True,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    테넌시의 Compartment 트리 조회 (+ 이름 필터)

    tenancy_id를 주지 않으면 OCI config의 tenancy OCID를 자동 사용합니다.
    """
    try:
        identity = oci_manager.identity(region=region)

        tid = tenancy_id or oci_manager.config.get("tenancy") or ""
        if not tid.startswith("ocid1.tenancy"):
            return {"error": "tenancy_id not provided and not found in OCI config."}

        resp = identity.list_compartments(
            compartment_id=tid,
            compartment_id_in_subtree=True,
            access_level="ACCESSIBLE",
        )
        items = [to_dict(x) for x in (resp.data or [])]

        if include_root:
            root = identity.get_compartment(compartment_id=tid)
            items.insert(0, to_dict(root.data))

        if name_contains:
            key = name_contains.lower()
            items = [c for c in items if (c.get("name") or "").lower().find(key) >= 0]

        return {"tenancy_id": tid, "count": len(items), "compartments": items}

    except oci.exceptions.ServiceError as e:
        log.exception("list_compartments_tree ServiceError")
        return {"error": f"ServiceError {e.status} {e.code}: {e.message}"}
    except Exception as e:
        log.exception("list_compartments_tree failed")
        return {"error": str(e)}


@mcp.tool()
def list_existing_resources_in_compartment(
    compartment_id: str,
    limit_total: int = 500,
    region: Optional[str] = None,
) -> Dict[str, Any]:
    """
    특정 Compartment에서 '실제 존재하는' 리소스만 조회합니다.

    - existing: 실제 존재(검증 통과)
    - stale: 404로 확인된 삭제 잔재(인덱스 잔재)
    - unverified: 타입 매핑/권한/기타로 검증 불가

    주의:
      - Resource Search 자체가 모든 리소스를 100% 보장하지 않습니다.
      - limit_total은 최대 조회 개수 제한입니다(기본 500).
    """
    try:
        search_client = oci_manager.search(region=region)
        query = f"query all resources where compartmentId = '{compartment_id}'"

        details = oci.resource_search.models.StructuredSearchDetails(
            query=query,
            type="Structured",
            matching_context_type="NONE",
        )

        candidates = _paginate_search(search_client, details, limit_total=limit_total)

        existing: List[Dict[str, Any]] = []
        stale: List[Dict[str, Any]] = []
        unverified: List[Dict[str, Any]] = []

        for it in candidates:
            status, reason = verify_exists(it, region=region)
            if status == "exists":
                existing.append(it)
            elif status == "stale":
                x = dict(it)
                x["_verify_reason"] = reason
                stale.append(x)
            else:
                x = dict(it)
                x["_verify_reason"] = reason
                unverified.append(x)

        # existing 타입별 요약 (소문자 기준으로 집계)
        counts: Dict[str, int] = {}
        for it in existing:
            rt, _ = _rtype_rid(it)
            rt = rt or "unknown"
            counts[rt] = counts.get(rt, 0) + 1
        summary = [
            {"resource_type": k, "count": v}
            for k, v in sorted(counts.items(), key=lambda x: (-x[1], x[0]))
        ]

        return {
            "compartment_id": compartment_id,
            "query": query,
            "limit_total": limit_total,
            "total_candidates": len(candidates),
            "existing_count": len(existing),
            "stale_count": len(stale),
            "unverified_count": len(unverified),
            "summary_existing": summary,
            "existing": existing,
            "stale": stale,
            "unverified": unverified,
        }

    except oci.exceptions.ServiceError as e:
        log.exception("list_existing_resources_in_compartment ServiceError")
        return {"compartment_id": compartment_id, "error": f"ServiceError {e.status} {e.code}: {e.message}"}
    except Exception as e:
        log.exception("list_existing_resources_in_compartment failed")
        return {"compartment_id": compartment_id, "error": str(e)}


if __name__ == "__main__":
    mcp.run()
