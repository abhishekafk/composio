"""Pydantic models and Enums for the Composio 100-App Research Agent."""

from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field


class Category(str, Enum):
    crm_sales = "crm_sales"
    support = "support"
    comms = "comms"
    marketing = "marketing"
    ecommerce = "ecommerce"
    data_seo_scraping = "data_seo_scraping"
    dev_infra = "dev_infra"
    productivity_pm = "productivity_pm"
    finance = "finance"
    ai_research_media = "ai_research_media"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class AuthMethod(str, Enum):
    oauth2 = "oauth2"
    api_key = "api_key"
    bearer_token = "bearer_token"
    basic = "basic"
    oauth1 = "oauth1"
    jwt_service_account = "jwt_service_account"
    signed_request = "signed_request"
    mtls = "mtls"
    none = "none"
    other = "other"


class AccessModel(str, Enum):
    self_serve_free = "self_serve_free"
    self_serve_trial = "self_serve_trial"
    self_serve_paid_plan = "self_serve_paid_plan"
    approval_required = "approval_required"
    partner_or_contract = "partner_or_contract"
    customer_only = "customer_only"
    no_public_api = "no_public_api"
    unknown = "unknown"


class ApiType(str, Enum):
    rest = "rest"
    graphql = "graphql"
    grpc = "grpc"
    sdk_only = "sdk_only"
    cli_local = "cli_local"
    none = "none"


class ApiBreadth(str, Enum):
    broad = "broad"
    moderate = "moderate"
    narrow = "narrow"
    none = "none"
    unknown = "unknown"


class OfficialMcp(str, Enum):
    official = "official"
    community_only = "community_only"
    none_found = "none_found"
    unknown = "unknown"


class Buildability(str, Enum):
    buildable_now = "buildable_now"
    buildable_with_paid_account = "buildable_with_paid_account"
    buildable_after_approval = "buildable_after_approval"
    needs_partnership = "needs_partnership"
    not_buildable = "not_buildable"
    unknown = "unknown"


class PrimaryBlocker(str, Enum):
    none = "none"
    paid_plan = "paid_plan"
    app_review = "app_review"
    partnership = "partnership"
    enterprise_contract = "enterprise_contract"
    kyc = "kyc"
    no_api = "no_api"
    ambiguous_identity = "ambiguous_identity"
    unknown = "unknown"


class EvidenceItem(BaseModel):
    field: str
    url: str
    quote: str = Field(description="Verbatim excerpt from the fetched page, max 200 chars", max_length=200)
    retrieved_at: str
    is_official_domain: bool = False


class AppResearchResult(BaseModel):
    app: str
    category: Category
    vendor_url: str
    identity_confidence: Confidence = Confidence.high
    description: str = Field(description="Concise description <= 140 chars", max_length=140)
    auth_methods: List[AuthMethod] = Field(default_factory=list)
    access_model: AccessModel = AccessModel.unknown
    gate_detail: str = Field(default="", description="e.g. 'Lite plan+', 'App Review', 'Enterprise only'")
    api_type: List[ApiType] = Field(default_factory=list)
    api_breadth: ApiBreadth = ApiBreadth.unknown
    openapi_spec_url: Optional[str] = None
    official_mcp: OfficialMcp = OfficialMcp.unknown
    official_mcp_url: Optional[str] = None
    buildability: Buildability = Buildability.unknown
    primary_blocker: PrimaryBlocker = PrimaryBlocker.unknown
    evidence: List[EvidenceItem] = Field(default_factory=list)
    confidence: Confidence = Confidence.medium
    needs_human: bool = False


class AppInput(BaseModel):
    id: int
    app: str
    category: Category
    vendor_url: str
    hint: str
    notes: Optional[str] = ""
