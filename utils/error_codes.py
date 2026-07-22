"""Central error code constants used across stages."""

SE000 = "SE000"
SE001 = "SE001"
SE002 = "SE002"
SE003 = "SE003"
SE004 = "SE004"
SE005 = "SE005"

PR001 = "PR001"
PR002 = "PR002"
PR003 = "PR003"

TG000 = "TG000"
TG001 = "TG001"
TG002 = "TG002"
TG003 = "TG003"
TG004 = "TG004"
TG005 = "TG005"
TG006 = "TG006"

FW000 = "FW000"
FW001 = "FW001"
FW002 = "FW002"
FW003 = "FW003"
FW004 = "FW004"

GP000 = "GP000"
GP001 = "GP001"
GP002 = "GP002"

DB001 = "DB001"
DB002 = "DB002"
DB003 = "DB003"

DL001 = "DL001"
DL002 = "DL002"

BK000 = "BK000"
BK001 = "BK001"

EX001 = "EX001"

OP001 = "OP001"


ERROR_CODE_MESSAGES = {
    SE000: "Unexpected public search failure",
    SE001: "Telegram search request failed",
    SE002: "Search provider parse failed",
    SE003: "Search result is empty",
    SE004: "Search provider rate limited",
    SE005: "Search provider requires verification",
    PR001: "Provider response parse failed",
    PR002: "Telegram link parse failed",
    PR003: "Result normalization failed",
    TG000: "Unexpected Telegram operation failure",
    TG001: "Telegram authentication or permission failed",
    TG002: "Telegram code request failed",
    TG003: "Telegram two-step verification failed",
    TG004: "Telegram flood wait or rate limit",
    TG005: "Telegram service operation failed",
    TG006: "Telegram login input invalid",
    FW000: "Unexpected forwarding failure",
    FW001: "Forward target is invalid",
    FW002: "Forward source selection is invalid",
    FW003: "Forward service failed",
    FW004: "Forward result was skipped",
    GP000: "Unexpected group creation failure",
    GP001: "Group creation failed",
    GP002: "Group persistence failed",
    DB001: "Database initialization failed",
    DB002: "Database migration failed",
    DB003: "Database write failed",
    DL001: "Media download failed",
    DL002: "Media download skipped or unavailable",
    BK000: "Unexpected backup failure",
    BK001: "Backup service failed",
    EX001: "Export service failed",
    OP001: "Operation cancelled by user",
}


def describe_error_code(error_code: str) -> str:
    """Return a stable diagnostic description for a known error code."""
    return ERROR_CODE_MESSAGES.get(str(error_code or "").strip().upper(), "")


def is_known_error_code(error_code: str) -> bool:
    """Return whether the code is part of the centralized application registry."""
    return bool(describe_error_code(error_code))
