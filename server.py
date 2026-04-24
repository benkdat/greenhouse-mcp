"""
Greenhouse MCP Server
Exposes DAT's Greenhouse Harvest API data to Claude for recruiting intelligence,
pipeline visibility, hiring efficiency dashboards, and interview rescheduling workflows.
"""

import asyncio
import os
from statistics import mean, median
from typing import Optional
from datetime import datetime, timezone

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

# ── Constants ────────────────────────────────────────────────────────────────

HARVEST_BASE_URL = "https://harvest.greenhouse.io/v1"
API_KEY = os.environ.get("GREENHOUSE_API_KEY", "")

mcp = FastMCP("greenhouse_mcp")


# ── Shared API Helpers ────────────────────────────────────────────────────────

async def gh_get(path: str, params: Optional[dict] = None) -> dict | list:
    """Authenticated GET request to Greenhouse Harvest API."""
    if not API_KEY:
        raise ValueError("GREENHOUSE_API_KEY environment variable is not set.")
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{HARVEST_BASE_URL}{path}",
            params=params or {},
            auth=(API_KEY, ""),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        return response.json()


async def gh_get_all(path: str, params: Optional[dict] = None) -> list:
    """Fetch all pages from a paginated Greenhouse endpoint (max per_page=500)."""
    if not API_KEY:
        raise ValueError("GREENHOUSE_API_KEY environment variable is not set.")
    results = []
    page = 1
    base_params = {**(params or {}), "per_page": 500}
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            response = await client.get(
                f"{HARVEST_BASE_URL}{path}",
                params={**base_params, "page": page},
                auth=(API_KEY, ""),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not data:
                break
            results.extend(data)
            if len(data) < 500:
                break
            page += 1
    return results


async def _fetch_offer_for_app(app_id: int) -> Optional[dict]:
    """Return the most recent offer for an application, or None."""
    try:
        offers = await gh_get(f"/applications/{app_id}/offers")
        return offers[0] if offers else None
    except Exception:
        return None


def _handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "Error: Invalid API key. Check your GREENHOUSE_API_KEY environment variable."
        if code == 403:
            return "Error: Permission denied. Your API key may lack access to this resource."
        if code == 404:
            return "Error: Resource not found. Verify the ID is correct."
        if code == 429:
            return "Error: Rate limit hit. Wait a moment before retrying."
        return f"Error: Greenhouse API returned status {code}: {e.response.text}"
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Try again in a moment."
    if isinstance(e, ValueError):
        return f"Error: {e}"
    return f"Error: {type(e).__name__}: {e}"


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "N/A"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %d, %Y")
    except Exception:
        return iso


def _days_between(d1: Optional[str], d2: Optional[str]) -> Optional[float]:
    """Calendar days from d1 to d2. None if either is missing or d2 < d1."""
    if not d1 or not d2:
        return None
    try:
        dt1 = datetime.fromisoformat(d1.replace("Z", "+00:00"))
        dt2 = datetime.fromisoformat(d2.replace("Z", "+00:00"))
        delta = (dt2 - dt1).total_seconds() / 86400
        return delta if delta >= 0 else None
    except Exception:
        return None


def _avg(values: list[float]) -> Optional[float]:
    return round(mean(values), 1) if values else None


def _median(values: list[float]) -> Optional[float]:
    return round(median(values), 1) if values else None


def _fmt_days(d: Optional[float]) -> str:
    return f"{d:.0f}d" if d is not None else "N/A"


# ── Input Models ─────────────────────────────────────────────────────────────

class GetJobInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: int = Field(..., description="Greenhouse job ID (numeric)", gt=0)


class GetCandidateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    candidate_id: int = Field(..., description="Greenhouse candidate ID (numeric)", gt=0)


class SearchCandidatesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    query: str = Field(
        ...,
        description="Search term matched against candidate name or email address.",
        min_length=1,
    )
    per_page: Optional[int] = Field(default=25, ge=1, le=500)
    page: Optional[int] = Field(default=1, ge=1)


class GetApplicationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    application_id: int = Field(..., description="Greenhouse application ID (numeric)", gt=0)


class ListJobsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: Optional[str] = Field(
        default="open",
        description="Filter by job status: 'open', 'closed', 'draft', or 'all'. Defaults to 'open'."
    )
    department_id: Optional[int] = Field(
        default=None,
        description="Filter by Greenhouse department ID. Use greenhouse_list_departments to find IDs."
    )
    opened_after: Optional[str] = Field(
        default=None,
        description="Only return jobs opened on or after this date (ISO 8601, e.g. '2025-01-01T00:00:00Z')."
    )
    per_page: Optional[int] = Field(default=50, ge=1, le=500)
    page: Optional[int] = Field(default=1, ge=1)


class ListApplicationsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: Optional[int] = Field(default=None, description="Filter applications by job ID.")
    status: Optional[str] = Field(
        default=None,
        description="Filter by status: 'active', 'rejected', 'hired'."
    )
    applied_after: Optional[str] = Field(
        default=None,
        description="Only return applications submitted on or after this date (ISO 8601)."
    )
    per_page: Optional[int] = Field(default=50, ge=1, le=500)
    page: Optional[int] = Field(default=1, ge=1)


class GetScheduledInterviewsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    application_id: Optional[int] = Field(
        default=None,
        description="Filter by application ID to get interviews for a specific candidate."
    )
    start_after: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime — only return interviews starting after this time (e.g. '2026-04-01T00:00:00Z')."
    )
    start_before: Optional[str] = Field(
        default=None,
        description="ISO 8601 datetime — only return interviews starting before this time."
    )
    per_page: Optional[int] = Field(default=50, ge=1, le=500)
    page: Optional[int] = Field(default=1, ge=1)


class GetScorecardsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    application_id: int = Field(..., description="Application ID to retrieve scorecards for.", gt=0)


class GetOfferInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    application_id: int = Field(..., description="Application ID to retrieve offer details for.", gt=0)


class PipelineSummaryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: int = Field(..., description="Greenhouse job ID to summarize pipeline for.", gt=0)


class ListUsersInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    email: Optional[str] = Field(
        default=None,
        description="Filter by email address to find a specific Greenhouse user."
    )
    per_page: Optional[int] = Field(default=50, ge=1, le=500)


class StalePipelineInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    days: int = Field(default=7, ge=1, le=365,
        description="Report applications with no activity in this many days. Default: 7.")
    job_id: Optional[int] = Field(default=None, description="Optionally limit to a single job ID.")


class ListDepartmentsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name_filter: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring to filter department names (e.g. 'product', 'data', 'convoy')."
    )


class HiringEfficiencyInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_ids: Optional[list[int]] = Field(
        default=None,
        description="Specific Greenhouse job IDs to analyze. If omitted, uses department_ids."
    )
    department_ids: Optional[list[int]] = Field(
        default=None,
        description="Filter all jobs by these department IDs. Use greenhouse_list_departments to find IDs."
    )
    opened_after: Optional[str] = Field(
        default=None,
        description="Only analyze jobs opened on or after this ISO 8601 date (e.g. '2025-01-01')."
    )
    include_open: bool = Field(
        default=True,
        description="Include still-open reqs (no TTF, but shows pipeline depth and time-in-flight)."
    )


class ReqDashboardInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    department_names: Optional[list[str]] = Field(
        default=None,
        description=(
            "Department name substrings to include (case-insensitive). "
            "Defaults to Convoy Platform teams: ['Product Tech', 'Data']. "
            "Pass a list to override, e.g. ['Engineering', 'Data Science']."
        )
    )
    opened_after: Optional[str] = Field(
        default=None,
        description="Only include reqs opened on or after this ISO 8601 date (e.g. '2025-01-01')."
    )
    status: Optional[str] = Field(
        default="all",
        description="Job status to include: 'open', 'closed', or 'all'. Default: 'all'."
    )


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool(
    name="greenhouse_list_departments",
    annotations={
        "title": "List Greenhouse Departments",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_list_departments(params: ListDepartmentsInput) -> str:
    """List all departments in Greenhouse with their IDs.

    Use this to discover department IDs before filtering jobs or running
    efficiency reports. Supports substring filtering by department name.

    Args:
        params (ListDepartmentsInput):
            - name_filter (Optional[str]): Case-insensitive name substring.

    Returns:
        str: Markdown list of departments with IDs.
    """
    try:
        departments = await gh_get_all("/departments")

        if not departments:
            return "No departments found."

        if params.name_filter:
            f = params.name_filter.lower()
            departments = [d for d in departments if f in d.get("name", "").lower()]

        if not departments:
            return f"No departments found matching '{params.name_filter}'."

        lines = ["## Greenhouse Departments\n"]
        for d in sorted(departments, key=lambda x: x.get("name", "")):
            parent = d.get("parent_id")
            child_ids = [c["id"] for c in d.get("child_ids", [])]
            line = f"- **{d['name']}** (ID: {d['id']})"
            if parent:
                line += f" | Parent ID: {parent}"
            if child_ids:
                line += f" | Child IDs: {child_ids}"
            lines.append(line)

        lines.append(f"\n_{len(departments)} department(s) returned_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_list_jobs",
    annotations={
        "title": "List Greenhouse Jobs",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_list_jobs(params: ListJobsInput) -> str:
    """List job requisitions from Greenhouse with optional filtering.

    Returns job title, ID, department, hiring managers, recruiter, and status.
    Use this to get an overview of the recruiting pipeline or find a specific
    job ID for deeper queries.

    Args:
        params (ListJobsInput):
            - status (str): 'open', 'closed', 'draft', or 'all'. Default: 'open'.
            - department_id (Optional[int]): Filter by department ID.
            - opened_after (Optional[str]): ISO 8601 date lower bound.
            - per_page (int): Results per page (max 500). Default: 50.
            - page (int): Page number. Default: 1.

    Returns:
        str: Markdown-formatted list of jobs with key metadata.
    """
    try:
        query: dict = {"per_page": params.per_page, "page": params.page}
        if params.status and params.status != "all":
            query["status"] = params.status
        if params.department_id:
            query["department_id"] = params.department_id
        if params.opened_after:
            query["opened_after"] = params.opened_after

        jobs = await gh_get("/jobs", params=query)

        if not jobs:
            return "No jobs found matching your filters."

        lines = [f"## Greenhouse Jobs ({params.status or 'all'})\n"]
        for job in jobs:
            dept = job.get("departments", [{}])[0].get("name", "No Department")
            offices = ", ".join(o.get("name", "") for o in job.get("offices", []))
            hiring_managers = [u["name"] for u in job.get("hiring_team", {}).get("hiring_managers", [])]
            recruiters = [u["name"] for u in job.get("hiring_team", {}).get("recruiters", [])]
            lines.append(
                f"### {job['name']} (ID: {job['id']})\n"
                f"- **Status**: {job.get('status', 'N/A')}\n"
                f"- **Department**: {dept}\n"
                f"- **Office(s)**: {offices or 'N/A'}\n"
                f"- **Hiring Manager(s)**: {', '.join(hiring_managers) or 'N/A'}\n"
                f"- **Recruiter(s)**: {', '.join(recruiters) or 'N/A'}\n"
                f"- **Opened**: {_fmt_date(job.get('opened_at'))}\n"
                f"- **Closed**: {_fmt_date(job.get('closed_at'))}\n"
                f"- **Notes**: {job.get('notes') or 'None'}\n"
            )

        lines.append(f"\n_Page {params.page} | {len(jobs)} jobs returned_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_job",
    annotations={
        "title": "Get Job Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_job(params: GetJobInput) -> str:
    """Get full details for a single Greenhouse job requisition.

    Returns complete job info including interview stages, hiring team,
    custom fields, and job description content.

    Args:
        params (GetJobInput):
            - job_id (int): Greenhouse job ID.

    Returns:
        str: Markdown-formatted job details.
    """
    try:
        job = await gh_get(f"/jobs/{params.job_id}")
        dept = job.get("departments", [{}])[0].get("name", "No Department")
        offices = ", ".join(o.get("name", "") for o in job.get("offices", []))
        stages = [s["name"] for s in job.get("stages", [])]
        hiring_managers = [u["name"] for u in job.get("hiring_team", {}).get("hiring_managers", [])]
        recruiters = [u["name"] for u in job.get("hiring_team", {}).get("recruiters", [])]
        coordinators = [u["name"] for u in job.get("hiring_team", {}).get("coordinators", [])]

        return (
            f"## {job['name']} (ID: {job['id']})\n\n"
            f"- **Status**: {job.get('status')}\n"
            f"- **Department**: {dept}\n"
            f"- **Office(s)**: {offices or 'N/A'}\n"
            f"- **Opened**: {_fmt_date(job.get('opened_at'))}\n"
            f"- **Closed**: {_fmt_date(job.get('closed_at'))}\n\n"
            f"### Hiring Team\n"
            f"- **Hiring Manager(s)**: {', '.join(hiring_managers) or 'N/A'}\n"
            f"- **Recruiter(s)**: {', '.join(recruiters) or 'N/A'}\n"
            f"- **Coordinator(s)**: {', '.join(coordinators) or 'N/A'}\n\n"
            f"### Interview Stages\n"
            + "\n".join(f"- {s}" for s in stages) + "\n\n"
            f"### Notes\n{job.get('notes') or 'None'}\n"
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_candidate",
    annotations={
        "title": "Get Candidate Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_candidate(params: GetCandidateInput) -> str:
    """Get full details for a Greenhouse candidate including all applications.

    Returns contact info, recruiter, source, tags, current applications
    and their stages, and any attachments.

    Args:
        params (GetCandidateInput):
            - candidate_id (int): Greenhouse candidate ID.

    Returns:
        str: Markdown-formatted candidate profile.
    """
    try:
        c = await gh_get(f"/candidates/{params.candidate_id}")
        name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        emails = [e["value"] for e in c.get("email_addresses", [])]
        phones = [p["value"] for p in c.get("phone_numbers", [])]
        recruiter = c.get("recruiter", {})
        coordinator = c.get("coordinator", {})
        tags = [t["name"] for t in c.get("tags", [])]

        apps_lines = []
        for app in c.get("applications", []):
            job_name = app.get("jobs", [{}])[0].get("name", "Unknown Role")
            stage = app.get("current_stage", {}).get("name", "N/A")
            status = app.get("status", "N/A")
            apps_lines.append(f"  - **{job_name}** | Stage: {stage} | Status: {status} | App ID: {app['id']}")

        return (
            f"## {name} (Candidate ID: {c['id']})\n\n"
            f"- **Email(s)**: {', '.join(emails) or 'N/A'}\n"
            f"- **Phone(s)**: {', '.join(phones) or 'N/A'}\n"
            f"- **Recruiter**: {recruiter.get('name', 'N/A')}\n"
            f"- **Coordinator**: {coordinator.get('name', 'N/A')}\n"
            f"- **Source**: {c.get('source', {}).get('public_name', 'N/A')}\n"
            f"- **Tags**: {', '.join(tags) or 'None'}\n"
            f"- **Added**: {_fmt_date(c.get('created_at'))}\n\n"
            f"### Applications\n"
            + ("\n".join(apps_lines) if apps_lines else "  No applications found.") + "\n"
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_search_candidates",
    annotations={
        "title": "Search Candidates",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_search_candidates(params: SearchCandidatesInput) -> str:
    """Search for candidates by name or email address.

    Use this when you know a candidate's name but not their Greenhouse ID.
    Returns matching candidates with their IDs, emails, and active application
    stages so you can drill into a specific candidate with greenhouse_get_candidate.

    Args:
        params (SearchCandidatesInput):
            - query (str): Name or email to search for.
            - per_page (int): Results per page. Default: 25.
            - page (int): Page number. Default: 1.

    Returns:
        str: Markdown list of matching candidates.
    """
    try:
        candidates = await gh_get(
            "/candidates",
            params={"query": params.query, "per_page": params.per_page, "page": params.page},
        )

        if not candidates:
            return f"No candidates found matching '{params.query}'."

        lines = [f"## Candidates matching '{params.query}'\n"]
        for c in candidates:
            name = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip() or "Unknown"
            emails = [e["value"] for e in c.get("email_addresses", [])]
            apps = c.get("applications", [])
            active = [a for a in apps if a.get("status") == "active"]
            app_summary = ", ".join(
                f"{a.get('jobs', [{}])[0].get('name', 'Unknown')} ({a.get('current_stage', {}).get('name', 'N/A')})"
                for a in active
            ) or "No active applications"
            lines.append(
                f"- **{name}** (ID: {c['id']}) | Email: {', '.join(emails) or 'N/A'} | "
                f"Active: {app_summary}"
            )

        lines.append(f"\n_Page {params.page} | {len(candidates)} results_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_list_applications",
    annotations={
        "title": "List Applications",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_list_applications(params: ListApplicationsInput) -> str:
    """List candidate applications, optionally filtered by job, status, or date.

    Returns candidate name, current stage, application status, recruiter,
    and application ID. Use this to see who is in a pipeline for a given req.

    Args:
        params (ListApplicationsInput):
            - job_id (Optional[int]): Filter by job ID.
            - status (Optional[str]): 'active', 'rejected', or 'hired'.
            - applied_after (Optional[str]): ISO 8601 date lower bound.
            - per_page (int): Results per page. Default: 50.
            - page (int): Page number. Default: 1.

    Returns:
        str: Markdown list of applications with stage and status.
    """
    try:
        query: dict = {"per_page": params.per_page, "page": params.page}
        if params.job_id:
            query["job_id"] = params.job_id
        if params.status:
            query["status"] = params.status
        if params.applied_after:
            query["applied_after"] = params.applied_after

        apps = await gh_get("/applications", params=query)

        if not apps:
            return "No applications found matching your filters."

        lines = ["## Applications\n"]
        for app in apps:
            candidate = app.get("candidate", {})
            name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Unknown"
            stage = app.get("current_stage", {}).get("name", "N/A")
            job_name = app.get("jobs", [{}])[0].get("name", "Unknown Role")
            recruiter = app.get("recruiter", {}).get("name", "N/A")
            lines.append(
                f"- **{name}** | Role: {job_name} | Stage: {stage} | "
                f"Status: {app.get('status', 'N/A')} | Recruiter: {recruiter} | "
                f"Applied: {_fmt_date(app.get('applied_at'))} | App ID: {app['id']}"
            )

        lines.append(f"\n_Page {params.page} | {len(apps)} applications returned_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_application",
    annotations={
        "title": "Get Application Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_application(params: GetApplicationInput) -> str:
    """Get full details for a single application including stage, rejection reason, and offer info.

    Args:
        params (GetApplicationInput):
            - application_id (int): Greenhouse application ID.

    Returns:
        str: Markdown-formatted application details.
    """
    try:
        app = await gh_get(f"/applications/{params.application_id}")
        candidate = app.get("candidate", {})
        name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Unknown"
        job_name = app.get("jobs", [{}])[0].get("name", "Unknown Role")
        stage = app.get("current_stage", {}).get("name", "N/A")
        rejection = app.get("rejection_reason", {})

        return (
            f"## Application: {name} → {job_name}\n\n"
            f"- **Application ID**: {app['id']}\n"
            f"- **Candidate ID**: {candidate.get('id', 'N/A')}\n"
            f"- **Status**: {app.get('status', 'N/A')}\n"
            f"- **Current Stage**: {stage}\n"
            f"- **Recruiter**: {app.get('recruiter', {}).get('name', 'N/A')}\n"
            f"- **Coordinator**: {app.get('coordinator', {}).get('name', 'N/A')}\n"
            f"- **Source**: {app.get('source', {}).get('public_name', 'N/A')}\n"
            f"- **Applied**: {_fmt_date(app.get('applied_at'))}\n"
            f"- **Last Activity**: {_fmt_date(app.get('last_activity_at'))}\n"
            f"- **Rejection Reason**: {rejection.get('name', 'N/A') if rejection else 'N/A'}\n"
        )

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_scheduled_interviews",
    annotations={
        "title": "Get Scheduled Interviews",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_scheduled_interviews(params: GetScheduledInterviewsInput) -> str:
    """Get scheduled interviews from Greenhouse, optionally filtered by application or date range.

    Returns interview name, start/end time, interviewers with email addresses
    (for Google Calendar cross-referencing), and video conferencing links.
    This is the key tool for rescheduling workflows — combine with Google Calendar
    to find mutual availability across the interview panel.

    Args:
        params (GetScheduledInterviewsInput):
            - application_id (Optional[int]): Filter by application.
            - start_after (Optional[str]): ISO 8601 start bound.
            - start_before (Optional[str]): ISO 8601 end bound.
            - per_page (int): Results per page. Default: 50.
            - page (int): Page number. Default: 1.

    Returns:
        str: Markdown list of interviews with interviewers and their emails.
    """
    try:
        query: dict = {"per_page": params.per_page, "page": params.page}
        if params.application_id:
            query["application_id"] = params.application_id
        if params.start_after:
            query["starts_after"] = params.start_after
        if params.start_before:
            query["starts_before"] = params.start_before

        interviews = await gh_get("/scheduled_interviews", params=query)

        if not interviews:
            return "No scheduled interviews found matching your filters."

        lines = ["## Scheduled Interviews\n"]
        for iv in interviews:
            interviewers = iv.get("interviewers", [])
            panel_lines = [
                f"  - {i.get('name', 'N/A')} ({i.get('email', 'N/A')}) — Status: {i.get('response_status', 'N/A')}"
                for i in interviewers
            ]
            lines.append(
                f"### {iv.get('name', 'Interview')} (ID: {iv['id']})\n"
                f"- **Application ID**: {iv.get('application_id', 'N/A')}\n"
                f"- **Start**: {iv.get('start', {}).get('date_time', 'N/A')}\n"
                f"- **End**: {iv.get('end', {}).get('date_time', 'N/A')}\n"
                f"- **Location/Link**: {iv.get('location') or 'N/A'}\n"
                f"- **Status**: {iv.get('status', 'N/A')}\n"
                f"- **Interview Panel**:\n" + "\n".join(panel_lines or ["  None listed"])
                + "\n"
            )

        lines.append(f"\n_Page {params.page} | {len(interviews)} interviews returned_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_scorecards",
    annotations={
        "title": "Get Interview Scorecards",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_scorecards(params: GetScorecardsInput) -> str:
    """Get all submitted interview scorecards for a candidate application.

    Returns interviewer name, overall recommendation, attribute ratings,
    and any written notes. Useful for assessing calibration across interviewers.

    Args:
        params (GetScorecardsInput):
            - application_id (int): Application ID.

    Returns:
        str: Markdown-formatted scorecards grouped by interviewer.
    """
    try:
        scorecards = await gh_get(f"/applications/{params.application_id}/scorecards")

        if not scorecards:
            return f"No scorecards submitted yet for application {params.application_id}."

        lines = [f"## Scorecards — Application {params.application_id}\n"]
        for sc in scorecards:
            interviewer = sc.get("interviewer", {}).get("name", "Unknown")
            overall = sc.get("overall_recommendation", "N/A")
            submitted = _fmt_date(sc.get("submitted_at"))
            attributes = sc.get("attributes", [])
            attr_lines = [f"  - {a.get('name', 'N/A')}: {a.get('rating', 'N/A')}" for a in attributes]
            notes = sc.get("notes") or "No notes provided."

            lines.append(
                f"### {interviewer} — {submitted}\n"
                f"- **Overall**: {overall}\n"
                f"- **Attributes**:\n" + "\n".join(attr_lines or ["  None"]) + "\n"
                f"- **Notes**: {notes}\n"
            )

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_get_offer",
    annotations={
        "title": "Get Offer Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_get_offer(params: GetOfferInput) -> str:
    """Get offer details for a candidate application.

    Returns offer status, compensation details, start date, and approval
    chain status. Useful for tracking where offers are in the approval flow.

    Args:
        params (GetOfferInput):
            - application_id (int): Application ID.

    Returns:
        str: Markdown-formatted offer details.
    """
    try:
        offers = await gh_get(f"/applications/{params.application_id}/offers")

        if not offers:
            return f"No offers found for application {params.application_id}."

        lines = [f"## Offers — Application {params.application_id}\n"]
        for offer in offers:
            custom_fields = offer.get("custom_fields", {})
            lines.append(
                f"### Offer ID: {offer['id']}\n"
                f"- **Status**: {offer.get('status', 'N/A')}\n"
                f"- **Created**: {_fmt_date(offer.get('created_at'))}\n"
                f"- **Sent**: {_fmt_date(offer.get('sent_at'))}\n"
                f"- **Resolved**: {_fmt_date(offer.get('resolved_at'))}\n"
                f"- **Start Date**: {offer.get('starts_at') or 'N/A'}\n"
            )
            if custom_fields:
                lines.append("- **Custom Fields**:")
                for k, v in custom_fields.items():
                    lines.append(f"  - {k}: {v}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_pipeline_summary",
    annotations={
        "title": "Pipeline Summary for a Job",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_pipeline_summary(params: PipelineSummaryInput) -> str:
    """Get a stage-by-stage pipeline summary for a specific job req.

    Fetches all active applications for the job and groups them by current
    interview stage. Gives a quick snapshot of where candidates stand —
    equivalent to a TalentWall board view.

    Args:
        params (PipelineSummaryInput):
            - job_id (int): Greenhouse job ID.

    Returns:
        str: Markdown pipeline summary grouped by stage.
    """
    try:
        job = await gh_get(f"/jobs/{params.job_id}")
        job_name = job.get("name", f"Job {params.job_id}")

        apps = await gh_get_all("/applications", params={"job_id": params.job_id, "status": "active"})

        if not apps:
            return f"No active applications found for {job_name}."

        stages: dict[str, list[str]] = {}
        for app in apps:
            stage = app.get("current_stage", {}).get("name", "Unknown Stage")
            candidate = app.get("candidate", {})
            name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip() or "Unknown"
            stages.setdefault(stage, []).append(f"{name} (App: {app['id']})")

        lines = [f"## Pipeline: {job_name}\n", f"**{len(apps)} active candidate(s)**\n"]
        for stage_name, candidates in stages.items():
            lines.append(f"### {stage_name} ({len(candidates)})")
            lines.extend(f"- {c}" for c in candidates)
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_list_users",
    annotations={
        "title": "List Greenhouse Users",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_list_users(params: ListUsersInput) -> str:
    """List Greenhouse users (interviewers, hiring managers, recruiters).

    Returns name, email, and role. Useful for cross-referencing Greenhouse
    interviewers with Google Calendar email addresses for scheduling.

    Args:
        params (ListUsersInput):
            - email (Optional[str]): Filter by email to find a specific user.
            - per_page (int): Results per page. Default: 50.

    Returns:
        str: Markdown list of users with names and emails.
    """
    try:
        query: dict = {"per_page": params.per_page}
        if params.email:
            query["email"] = params.email

        users = await gh_get("/users", params=query)

        if not users:
            return "No users found."

        lines = ["## Greenhouse Users\n"]
        for u in users:
            role = "Disabled" if u.get("disabled") else "Active"
            lines.append(
                f"- **{u.get('name', 'N/A')}** | Email: {u.get('primary_email_address', 'N/A')} | "
                f"Site Admin: {u.get('site_admin', False)} | Status: {role} | ID: {u['id']}"
            )

        lines.append(f"\n_{len(users)} users returned_")
        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_stale_pipeline_report",
    annotations={
        "title": "Stale Pipeline Report",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_stale_pipeline_report(params: StalePipelineInput) -> str:
    """Find active applications with no activity in the last N days.

    Surfaces candidates who may be stuck or forgotten in the pipeline —
    useful for weekly recruiter pipeline reviews. Scans all active
    applications (not just the first page).

    Args:
        params (StalePipelineInput):
            - days (int): Inactivity threshold in days. Default: 7.
            - job_id (Optional[int]): Limit report to a specific job.

    Returns:
        str: Markdown report of stale applications sorted by longest inactive first.
    """
    try:
        query: dict = {"status": "active"}
        if params.job_id:
            query["job_id"] = params.job_id

        apps = await gh_get_all("/applications", params=query)

        if not apps:
            return "No active applications found."

        now = datetime.now(timezone.utc)
        stale = []
        for app in apps:
            last_activity = app.get("last_activity_at")
            if not last_activity:
                continue
            try:
                last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                days_stale = (now - last_dt).days
            except ValueError:
                continue
            if days_stale >= params.days:
                candidate = app.get("candidate", {})
                name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
                job_name = app.get("jobs", [{}])[0].get("name", "Unknown Role")
                stage = app.get("current_stage", {}).get("name", "N/A")
                recruiter = app.get("recruiter", {}).get("name", "N/A")
                stale.append((days_stale, name, job_name, stage, recruiter, app["id"]))

        if not stale:
            return f"No stale applications found (all have had activity in the last {params.days} days)."

        stale.sort(key=lambda x: x[0], reverse=True)
        lines = [
            f"## Stale Pipeline Report\n"
            f"_{len(stale)} application(s) with no activity in {params.days}+ days "
            f"(scanned {len(apps)} active total)_\n"
        ]
        for days, name, job, stage, recruiter, app_id in stale:
            lines.append(
                f"- **{name}** | {job} | Stage: {stage} | Recruiter: {recruiter} | "
                f"**{days} days since last activity** | App ID: {app_id}"
            )

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


# ── Efficiency helpers ────────────────────────────────────────────────────────

async def _efficiency_for_job(job: dict) -> dict:
    """
    Collect all applications for a job and compute efficiency metrics.
    Returns a dict with job metadata, counts, and timing stats.
    """
    job_id = job["id"]
    job_name = job.get("name", f"Job {job_id}")
    dept = job.get("departments", [{}])[0].get("name", "N/A")
    recruiters = [u["name"] for u in job.get("hiring_team", {}).get("recruiters", [])]
    hms = [u["name"] for u in job.get("hiring_team", {}).get("hiring_managers", [])]
    opened_at = job.get("opened_at")
    closed_at = job.get("closed_at")
    status = job.get("status", "N/A")

    # Fetch all applications for this job (all statuses)
    all_apps = await gh_get_all("/applications", params={"job_id": job_id})

    total = len(all_apps)
    active_apps = [a for a in all_apps if a.get("status") == "active"]
    hired_apps = [a for a in all_apps if a.get("status") == "hired"]
    rejected_apps = [a for a in all_apps if a.get("status") == "rejected"]

    # Source breakdown (all apps)
    sources: dict[str, int] = {}
    for a in all_apps:
        src = a.get("source", {}).get("public_name", "Unknown") if a.get("source") else "Unknown"
        sources[src] = sources.get(src, 0) + 1

    # Time to Fill: job.opened_at → hired app's last_activity_at (proxy for hire date)
    ttf_values: list[float] = []
    for a in hired_apps:
        d = _days_between(opened_at, a.get("last_activity_at"))
        if d is not None:
            ttf_values.append(d)

    # Time in Process (apply → resolution) for hired + rejected apps
    tip_hired: list[float] = []
    for a in hired_apps:
        d = _days_between(a.get("applied_at"), a.get("last_activity_at"))
        if d is not None:
            tip_hired.append(d)

    tip_rejected: list[float] = []
    for a in rejected_apps:
        d = _days_between(a.get("applied_at"), a.get("last_activity_at"))
        if d is not None:
            tip_rejected.append(d)

    # Time to Offer, Time to Accept, Time to Start — need offer data for hired apps
    tto_values: list[float] = []   # applied_at → offer.sent_at
    tta_values: list[float] = []   # offer.sent_at → offer.resolved_at
    tts_values: list[float] = []   # offer.resolved_at → offer.starts_at

    if hired_apps:
        offers = await asyncio.gather(*[_fetch_offer_for_app(a["id"]) for a in hired_apps])
        for app, offer in zip(hired_apps, offers):
            if not offer:
                continue
            tto = _days_between(app.get("applied_at"), offer.get("sent_at"))
            if tto is not None:
                tto_values.append(tto)
            tta = _days_between(offer.get("sent_at"), offer.get("resolved_at"))
            if tta is not None:
                tta_values.append(tta)
            tts = _days_between(offer.get("resolved_at"), offer.get("starts_at"))
            if tts is not None:
                tts_values.append(tts)

    # Days open (for open reqs)
    days_open: Optional[float] = None
    if status == "open" and opened_at:
        days_open = _days_between(opened_at, datetime.now(timezone.utc).isoformat())

    return {
        "job_id": job_id,
        "job_name": job_name,
        "dept": dept,
        "status": status,
        "recruiters": recruiters,
        "hiring_managers": hms,
        "opened_at": opened_at,
        "closed_at": closed_at,
        "days_open": days_open,
        "total": total,
        "active": len(active_apps),
        "hired": len(hired_apps),
        "rejected": len(rejected_apps),
        "sources": sources,
        # TTF
        "ttf_avg": _avg(ttf_values),
        "ttf_median": _median(ttf_values),
        "ttf_values": ttf_values,
        # Time in process
        "tip_hired_avg": _avg(tip_hired),
        "tip_rejected_avg": _avg(tip_rejected),
        # Time to Offer
        "tto_avg": _avg(tto_values),
        "tto_median": _median(tto_values),
        # Time to Accept
        "tta_avg": _avg(tta_values),
        # Time to Start
        "tts_avg": _avg(tts_values),
    }


def _render_efficiency_row(r: dict) -> list[str]:
    """Render a single req's efficiency section as markdown lines."""
    lines = []
    status_badge = "🟢 Open" if r["status"] == "open" else "✅ Closed"
    lines.append(f"### {r['job_name']} (ID: {r['job_id']}) — {status_badge}")
    lines.append(f"- **Dept**: {r['dept']} | **Recruiter(s)**: {', '.join(r['recruiters']) or 'N/A'} | **HM**: {', '.join(r['hiring_managers']) or 'N/A'}")
    lines.append(f"- **Opened**: {_fmt_date(r['opened_at'])} | **Closed**: {_fmt_date(r['closed_at'])}")
    if r["days_open"] is not None:
        lines.append(f"- **Days Open So Far**: {r['days_open']:.0f}d")
    lines.append(f"- **Pipeline**: {r['total']} total | {r['active']} active | {r['hired']} hired | {r['rejected']} rejected")

    lines.append("#### Efficiency Metrics")
    lines.append(f"| Metric | Avg | Median |")
    lines.append(f"|--------|-----|--------|")
    lines.append(f"| TTF (req open → hire) | {_fmt_days(r['ttf_avg'])} | {_fmt_days(r['ttf_median'])} |")
    lines.append(f"| Time in Process — hired (apply → hire) | {_fmt_days(r['tip_hired_avg'])} | N/A |")
    lines.append(f"| Time in Process — rejected (apply → reject) | {_fmt_days(r['tip_rejected_avg'])} | N/A |")
    lines.append(f"| Time to Offer (apply → offer sent) | {_fmt_days(r['tto_avg'])} | {_fmt_days(r['tto_median'])} |")
    lines.append(f"| Time to Accept (offer sent → resolved) | {_fmt_days(r['tta_avg'])} | N/A |")
    lines.append(f"| Time to Start (offer resolved → start date) | {_fmt_days(r['tts_avg'])} | N/A |")

    if r["sources"]:
        top_sources = sorted(r["sources"].items(), key=lambda x: x[1], reverse=True)[:5]
        lines.append("#### Top Sources")
        for src, count in top_sources:
            lines.append(f"- {src}: {count}")

    lines.append("")
    return lines


@mcp.tool(
    name="greenhouse_hiring_efficiency",
    annotations={
        "title": "Hiring Efficiency by Req",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_hiring_efficiency(params: HiringEfficiencyInput) -> str:
    """Compute hiring efficiency metrics (TTF, time to offer, time to accept, time to start)
    for one or more reqs.

    For each req returns:
    - TTF (Time to Fill): days from req open → hire date
    - Time in Process (hired): days from application → hire decision
    - Time in Process (rejected): days from application → rejection
    - Time to Offer: days from application → offer sent
    - Time to Accept: days from offer sent → offer resolved
    - Time to Start: days from offer resolved → start date
    - Pipeline counts (total / active / hired / rejected)
    - Top candidate sources

    Also shows rolled-up averages across all analyzed reqs.

    Use greenhouse_list_departments to find department IDs.

    Args:
        params (HiringEfficiencyInput):
            - job_ids (Optional[list[int]]): Specific job IDs. Takes precedence over department_ids.
            - department_ids (Optional[list[int]]): Filter all jobs by these departments.
            - opened_after (Optional[str]): ISO 8601 date lower bound for job open date.
            - include_open (bool): Include open reqs. Default: True.

    Returns:
        str: Markdown efficiency report with per-req and summary tables.
    """
    try:
        if params.job_ids:
            jobs = await asyncio.gather(*[gh_get(f"/jobs/{jid}") for jid in params.job_ids])
            jobs = list(jobs)
        elif params.department_ids:
            all_jobs = []
            for dept_id in params.department_ids:
                q: dict = {"department_id": dept_id, "per_page": 500}
                if not params.include_open:
                    q["status"] = "closed"
                if params.opened_after:
                    q["opened_after"] = params.opened_after
                dept_jobs = await gh_get_all("/jobs", params=q)
                all_jobs.extend(dept_jobs)
            jobs = all_jobs
        else:
            return "Provide either job_ids or department_ids to scope the report."

        if not jobs:
            return "No jobs found matching your filters."

        if not params.include_open:
            jobs = [j for j in jobs if j.get("status") != "open"]

        if params.opened_after:
            cutoff = params.opened_after[:10]
            jobs = [j for j in jobs if j.get("opened_at", "") >= cutoff]

        if not jobs:
            return "No jobs remain after applying filters."

        # Parallel fetch efficiency data for all jobs
        results = await asyncio.gather(*[_efficiency_for_job(j) for j in jobs])
        results = sorted(results, key=lambda r: r["opened_at"] or "", reverse=True)

        lines = [
            f"# Hiring Efficiency Report\n",
            f"_{len(results)} req(s) analyzed_\n",
        ]

        # Rolled-up summary
        all_ttf = [v for r in results for v in r["ttf_values"]]
        all_tto = [r["tto_avg"] for r in results if r["tto_avg"] is not None]
        all_tta = [r["tta_avg"] for r in results if r["tta_avg"] is not None]
        total_hired = sum(r["hired"] for r in results)
        total_apps = sum(r["total"] for r in results)

        lines.append("## Summary\n")
        lines.append(f"| Metric | Avg | Median |")
        lines.append(f"|--------|-----|--------|")
        lines.append(f"| TTF across all hires | {_fmt_days(_avg(all_ttf))} | {_fmt_days(_median(all_ttf))} |")
        lines.append(f"| Time to Offer (apply → offer sent) | {_fmt_days(_avg(all_tto))} | {_fmt_days(_median(all_tto))} |")
        lines.append(f"| Time to Accept (offer sent → resolved) | {_fmt_days(_avg(all_tta))} | {_fmt_days(_median(all_tta))} |")
        lines.append(f"\n**Total applications**: {total_apps} | **Total hires**: {total_hired}\n")

        lines.append("\n---\n## By Req\n")
        for r in results:
            lines.extend(_render_efficiency_row(r))

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


@mcp.tool(
    name="greenhouse_req_dashboard",
    annotations={
        "title": "Req Dashboard — Convoy Platform (Product Tech & Data)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def greenhouse_req_dashboard(params: ReqDashboardInput) -> str:
    """Pre-configured hiring efficiency dashboard for Convoy Platform teams.

    Defaults to departments matching 'Product Tech' and 'Data'. Override with
    department_names to target other teams. Shows TTF, time to offer, time to accept,
    pipeline health, and source breakdown for every req in scope — rolled up with
    cross-req averages at the top.

    This is the single tool to run for a recruiting efficiency review meeting.

    Args:
        params (ReqDashboardInput):
            - department_names (Optional[list[str]]): Department name substrings.
              Default: ['Product Tech', 'Data'].
            - opened_after (Optional[str]): ISO 8601 date lower bound.
            - status (Optional[str]): 'open', 'closed', or 'all'. Default: 'all'.

    Returns:
        str: Full markdown efficiency dashboard.
    """
    try:
        target_names = params.department_names or ["Product Tech", "Data"]

        # Discover all departments and match by name substring
        all_depts = await gh_get_all("/departments")
        matched_depts = [
            d for d in all_depts
            if any(t.lower() in d.get("name", "").lower() for t in target_names)
        ]

        if not matched_depts:
            return (
                f"No departments found matching: {target_names}. "
                "Run greenhouse_list_departments to see all department names."
            )

        dept_names_found = [d["name"] for d in matched_depts]
        dept_ids = [d["id"] for d in matched_depts]

        # Fetch all jobs across matched departments
        job_queries = []
        for dept_id in dept_ids:
            q: dict = {"department_id": dept_id, "per_page": 500}
            if params.status and params.status != "all":
                q["status"] = params.status
            if params.opened_after:
                q["opened_after"] = params.opened_after
            job_queries.append(gh_get_all("/jobs", params=q))

        dept_job_lists = await asyncio.gather(*job_queries)
        jobs: list[dict] = []
        seen_ids: set[int] = set()
        for job_list in dept_job_lists:
            for j in job_list:
                if j["id"] not in seen_ids:
                    jobs.append(j)
                    seen_ids.add(j["id"])

        if not jobs:
            return (
                f"No jobs found for departments: {dept_names_found} "
                f"(status={params.status or 'all'}, opened_after={params.opened_after or 'any'})."
            )

        if params.opened_after:
            cutoff = params.opened_after[:10]
            jobs = [j for j in jobs if j.get("opened_at", "") >= cutoff]

        # Parallel efficiency fetch
        results = await asyncio.gather(*[_efficiency_for_job(j) for j in jobs])
        results = sorted(results, key=lambda r: r["opened_at"] or "", reverse=True)

        open_reqs = [r for r in results if r["status"] == "open"]
        closed_reqs = [r for r in results if r["status"] != "open"]
        all_ttf = [v for r in results for v in r["ttf_values"]]
        all_tto = [r["tto_avg"] for r in results if r["tto_avg"] is not None]
        all_tta = [r["tta_avg"] for r in results if r["tta_avg"] is not None]
        all_tts = [r["tts_avg"] for r in results if r["tts_avg"] is not None]
        total_hired = sum(r["hired"] for r in results)
        total_apps = sum(r["total"] for r in results)
        total_active = sum(r["active"] for r in results)

        # Source rollup across all reqs
        all_sources: dict[str, int] = {}
        for r in results:
            for src, cnt in r["sources"].items():
                all_sources[src] = all_sources.get(src, 0) + cnt
        top_sources = sorted(all_sources.items(), key=lambda x: x[1], reverse=True)[:8]

        lines = [
            f"# Convoy Platform Hiring Dashboard",
            f"**Departments**: {', '.join(dept_names_found)}",
            f"**Date filter**: opened after {params.opened_after or 'all time'} | **Status**: {params.status or 'all'}",
            f"**Generated**: {datetime.now(timezone.utc).strftime('%b %d, %Y')}\n",
            f"---\n## Summary\n",
            f"| | Count |",
            f"|---|---|",
            f"| Total reqs | {len(results)} |",
            f"| Open reqs | {len(open_reqs)} |",
            f"| Closed reqs | {len(closed_reqs)} |",
            f"| Total applications | {total_apps} |",
            f"| Active candidates | {total_active} |",
            f"| Total hires | {total_hired} |\n",
            f"### Efficiency Averages (across all hires)\n",
            f"| Metric | Avg | Median |",
            f"|--------|-----|--------|",
            f"| TTF (req open → hire) | {_fmt_days(_avg(all_ttf))} | {_fmt_days(_median(all_ttf))} |",
            f"| Time to Offer (apply → offer sent) | {_fmt_days(_avg(all_tto))} | {_fmt_days(_median(all_tto))} |",
            f"| Time to Accept (offer sent → resolved) | {_fmt_days(_avg(all_tta))} | {_fmt_days(_median(all_tta))} |",
            f"| Time to Start (offer resolved → start) | {_fmt_days(_avg(all_tts))} | {_fmt_days(_median(all_tts))} |\n",
        ]

        if top_sources:
            lines.append("### Top Sources (all reqs)\n")
            for src, cnt in top_sources:
                lines.append(f"- {src}: {cnt}")
            lines.append("")

        if open_reqs:
            lines.append(f"\n---\n## Open Reqs ({len(open_reqs)})\n")
            for r in open_reqs:
                lines.extend(_render_efficiency_row(r))

        if closed_reqs:
            lines.append(f"\n---\n## Closed Reqs ({len(closed_reqs)})\n")
            for r in closed_reqs:
                lines.extend(_render_efficiency_row(r))

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


# ── ASGI App (for uvicorn / Render) ──────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "tools": len(mcp._tool_manager._tools)})

_mcp_asgi = mcp.streamable_http_app()

app = Starlette(routes=[
    Route("/health", _health),
    Mount("/", _mcp_asgi),
])


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 8000)),
        )
