import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DeployStatus = Literal["draft", "success", "error"]

# ── Trust-Store (ManagedCertificate) ─────────────────────────────────────
CertAction = Literal["import", "internal", "external", "sign_csr", "import_csr", "reissue", "manual"]
# Curated subset of Trust/Cert.xml's key_type OptionValues (RSA sizes +
# EC curves) — see ManagedCertificate's docstring.
CertKeyType = Literal["2048", "3072", "4096", "prime256v1", "secp384r1", "secp521r1"]
CertDigest = Literal["sha1", "sha224", "sha256", "sha384", "sha512"]
# No "v3_ca": OPNsense would create a certificate with the CA extension, not a CA that can be used as `caref`
# (found by the 1.31.0 test run, T3). Creating a CA is not offered; the UI never did.
CertType = Literal["usr_cert", "server_cert", "combined_server_client"]

_DN_ACTIONS = {"internal", "external", "reissue"}
_CRT_ACTIONS = {"import", "import_csr", "manual"}


class ManagedCertificateBase(BaseModel):
    host_id: uuid.UUID
    action: CertAction
    description: str = Field(min_length=1, max_length=255)
    commonname: str = Field(default="", max_length=255)
    country: str = Field(default="DE", max_length=8)
    state: str = Field(default="", max_length=255)
    city: str = Field(default="", max_length=255)
    organization: str = Field(default="", max_length=255)
    email: str = Field(default="", max_length=255)
    key_type: CertKeyType = "2048"
    digest: CertDigest = "sha256"
    cert_type: CertType = "server_cert"
    lifetime: int = Field(default=397, ge=1, le=36500)
    caref: str | None = Field(default=None, max_length=64)
    csr_payload: str | None = Field(default=None, max_length=20000)
    crt_payload: str | None = Field(default=None, max_length=20000)

    @field_validator("description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()

    @model_validator(mode="after")
    def check_action_requirements(self):
        if self.action in _DN_ACTIONS and not self.commonname.strip():
            raise ValueError("Für diese Aktion wird ein Common Name benötigt")
        if self.action == "sign_csr" and not (self.csr_payload or "").strip():
            raise ValueError("Für 'CSR signieren' wird ein CSR (PEM) benötigt")
        if self.action in _CRT_ACTIONS and not (self.crt_payload or "").strip():
            raise ValueError("Für diese Aktion wird ein Zertifikat (PEM) benötigt")
        return self


class ManagedCertificateCreate(ManagedCertificateBase):
    # Only meaningful for action="import" — see cert_key_store.py. Never
    # returned by the API, only private_key_set.
    private_key: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def check_private_key_scope(self):
        if self.private_key and self.action != "import":
            raise ValueError("Ein privater Schlüssel kann nur bei Aktion 'import' hinterlegt werden")
        return self


class ManagedCertificateEdit(ManagedCertificateBase):
    expected_updated_at: datetime
    # None keeps the previously stored key unchanged (write-only, same
    # pattern as IpsecTunnelEdit.pre_shared_key).
    private_key: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def check_private_key_scope(self):
        if self.private_key and self.action != "import":
            raise ValueError("Ein privater Schlüssel kann nur bei Aktion 'import' hinterlegt werden")
        return self


class ManagedCertificateRead(ManagedCertificateBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    private_key_set: bool
    opnsense_uuid: str | None
    status: DeployStatus
    message: str | None
    deployed_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── ACME accounts ─────────────────────────────────────────────────────
AcmeCA = Literal[
    "letsencrypt", "letsencrypt_test", "zerossl", "buypass", "buypass_test", "google", "google_test", "sslcom", "custom"
]


class AcmeAccountBase(BaseModel):
    host_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=255)
    email: str = Field(default="", max_length=255)
    ca: AcmeCA = "letsencrypt"
    custom_ca: str = Field(default="", max_length=255)
    eab_kid: str = Field(default="", max_length=255)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()

    @model_validator(mode="after")
    def check_custom_ca(self):
        if self.ca == "custom" and not self.custom_ca.strip():
            raise ValueError("Für CA 'custom' wird eine Custom-CA-URL benötigt")
        return self


class AcmeAccountCreate(AcmeAccountBase):
    eab_hmac: str | None = Field(default=None, max_length=8192)


class AcmeAccountEdit(AcmeAccountBase):
    expected_updated_at: datetime
    eab_hmac: str | None = Field(default=None, max_length=8192)


class AcmeAccountRead(AcmeAccountBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    eab_hmac_set: bool
    opnsense_uuid: str | None
    status: DeployStatus
    message: str | None
    deployed_at: datetime | None
    register_status: str | None
    register_message: str | None
    register_at: datetime | None
    created_at: datetime
    updated_at: datetime


# ── ACME certificate requests ─────────────────────────────────────────
# Prefixed OptionValues, verified against AcmeClient.xml — see
# AcmeCertificateRequest's docstring for why this matters.
AcmeKeyLength = Literal["key_2048", "key_3072", "key_4096", "key_ec256", "key_ec384"]


class AcmeCertificateRequestBase(BaseModel):
    host_id: uuid.UUID
    name: str = Field(min_length=1, max_length=255)
    alt_names: str = Field(default="", max_length=2000)
    account_id: uuid.UUID
    validation_ref: str = Field(min_length=1, max_length=64)
    key_length: AcmeKeyLength = "key_4096"
    ocsp: bool = False
    auto_renewal: bool = True
    # Days SINCE LAST ISSUANCE, not days before expiry — see
    # AcmeCertificateRequest's docstring. MinimumValue/MaximumValue
    # verified against AcmeClient.xml's renewInterval field (0..5000).
    renew_interval: int = Field(default=60, ge=0, le=5000)
    enabled: bool = True

    @field_validator("name", "validation_ref")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class AcmeCertificateRequestCreate(AcmeCertificateRequestBase):
    pass


class AcmeCertificateRequestEdit(AcmeCertificateRequestBase):
    expected_updated_at: datetime


class AcmeCertificateRequestRead(AcmeCertificateRequestBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opnsense_uuid: str | None
    status: DeployStatus
    message: str | None
    deployed_at: datetime | None
    sign_status: str | None
    sign_message: str | None
    sign_triggered_at: datetime | None
    remote_status_code: int | None
    remote_status_last_update: datetime | None
    remote_last_update: datetime | None
    created_at: datetime
    updated_at: datetime
