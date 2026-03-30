"""
Greenhouse MCP Server
Exposes DAT's Greenhouse Harvest API data to Claude for recruiting intelligence,
pipeline visibility, and interview rescheduling workflows.
"""

import json
import os
from typing import Optional, List
from datetime import datetime

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ConfigDict

# ── Constants ────────────────────────────────────────────────────────────────

HARVEST_BASE_URL = "https://harvest.greenhouse.io/v1"
API_KEY = os.environ.get("GREENHOUSE_API_KEY", "")

mcp = FastMCP("greenhouse_mcp")


# ── Shared API Client ─────────────────────────────────────────────────────────

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


def _handle_error(e: Exception) -> str:
    """Return a clear, actionable error string."""
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
    """Format ISO date string to readable form."""
    if not iso:
        return "N/A"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %d, %Y")
    except Exception:
        return iso


# ── Input Models ─────────────────────────────────────────────────────────────

class GetJobInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: int = Field(..., description="Greenhouse job ID (numeric)", gt=0)


class GetCandidateInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    candidate_id: int = Field(..., description="Greenhouse candidate ID (numeric)", gt=0)


class GetApplicationInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    application_id: int = Field(..., description="Greenhouse application ID (numeric)", gt=0)


class ListJobsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: Optional[str] = Field(
        default="open",
        description="Filter by job status: 'open', 'closed', 'draft'. Defaults to 'open'."
    )
    department_id: Optional[int] = Field(
        default=None,
        description="Filter by Greenhouse department ID."
    )
    per_page: Optional[int] = Field(
        default=50,
        description="Results per page (max 500).",
        ge=1,
        le=500
    )
    page: Optional[int] = Field(
        default=1,
        description="Page number for pagination.",
        ge=1
    )


class ListApplicationsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    job_id: Optional[int] = Field(
        default=None,
        description="Filter applications by job ID."
    )
    status: Optional[str] = Field(
        default=None,
        description="Filter by status: 'active', 'rejected', 'hired'."
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


# ── Tools ─────────────────────────────────────────────────────────────────────

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
    """List open (or filtered) job requisitions from Greenhouse.

    Returns job title, ID, department, hiring managers, recruiter, status,
    and number of active applications per req. Use this to get an overview
    of the recruiting pipeline or find a specific job ID for deeper queries.

    Args:
        params (ListJobsInput):
            - status (str): 'open', 'closed', or 'draft'. Default: 'open'.
            - department_id (Optional[int]): Filter by department.
            - per_page (int): Results per page (max 500). Default: 50.
            - page (int): Page number. Default: 1.

    Returns:
        str: Markdown-formatted list of jobs with key metadata.
    """
    try:
        query: dict = {"per_page": params.per_page, "page": params.page}
        if params.status:
            query["status"] = params.status
        if params.department_id:
            query["department_id"] = params.department_id

        jobs = await gh_get("/jobs", params=query)

        if not jobs:
            return "No jobs found matching your filters."

        lines = [f"## Greenhouse Jobs ({params.status or 'all'})\n"]
        for job in jobs:
            dept = job.get("departments", [{}])[0].get("name", "No Department")
            offices = ", ".join(o.get("name", "") for o in job.get("offices", []))
            hiring_managers = [
                u["name"] for u in job.get("hiring_team", {}).get("hiring_managers", [])
            ]
            recruiters = [
                u["name"] for u in job.get("hiring_team", {}).get("recruiters", [])
            ]
            lines.append(
                f"### {job['name']} (ID: {job['id']})\n"
                f"- **Status**: {job.get('status', 'N/A')}\n"
                f"- **Department**: {dept}\n"
                f"- **Office(s)**: {offices or 'N/A'}\n"
                f"- **Hiring Manager(s)**: {', '.join(hiring_managers) or 'N/A'}\n"
                f"- **Recruiter(s)**: {', '.join(recruiters) or 'N/A'}\n"
                f"- **Opened**: {_fmt_date(job.get('opened_at'))}\n"
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
        hiring_managers = [
            u["name"] for u in job.get("hiring_team", {}).get("hiring_managers", [])
        ]
        recruiters = [
            u["name"] for u in job.get("hiring_team", {}).get("recruiters", [])
        ]
        coordinators = [
            u["name"] for u in job.get("hiring_team", {}).get("coordinators", [])
        ]

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
    """List candidate applications, optionally filtered by job or status.

    Returns candidate name, current stage, application status, recruiter,
    and application ID. Use this to see who is in a pipeline for a given req.

    Args:
        params (ListApplicationsInput):
            - job_id (Optional[int]): Filter by job ID.
            - status (Optional[str]): 'active', 'rejected', or 'hired'.
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
                f"Status: {app.get('status', 'N/A')} | Recruiter: {recruiter} | App ID: {app['id']}"
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

    Returns:
        str: Markdown list of interviews with interviewers and their emails.
    """
    try:
        query: dict = {"per_page": params.per_page}
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
            attr_lines = [
                f"  - {a.get('name', 'N/A')}: {a.get('rating', 'N/A')}"
                for a in attributes
            ]
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
        # Fetch job to get stage names
        job = await gh_get(f"/jobs/{params.job_id}")
        job_name = job.get("name", f"Job {params.job_id}")

        # Fetch active applications
        apps = await gh_get("/applications", params={"job_id": params.job_id, "status": "active", "per_page": 500})

        if not apps:
            return f"No active applications found for {job_name}."

        # Group by stage
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
async def greenhouse_stale_pipeline_report(params: ListJobsInput) -> str:
    """Find active applications with no activity in the last N days.

    Surfaces candidates who may be stuck or forgotten in the pipeline —
    useful for weekly recruiter pipeline reviews.

    Args:
        params (ListJobsInput):
            - per_page (int): Max applications to scan. Default: 50.
            - page (int): Page number.

    Returns:
        str: Markdown report of stale applications sorted by last activity date.
    """
    try:
        apps = await gh_get("/applications", params={
            "status": "active",
            "per_page": params.per_page,
            "page": params.page
        })

        if not apps:
            return "No active applications found."

        now = datetime.utcnow()
        stale = []
        for app in apps:
            last_activity = app.get("last_activity_at")
            if last_activity:
                last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00")).replace(tzinfo=None)
                days_stale = (now - last_dt).days
                if days_stale >= 7:
                    candidate = app.get("candidate", {})
                    name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
                    job_name = app.get("jobs", [{}])[0].get("name", "Unknown Role")
                    stage = app.get("current_stage", {}).get("name", "N/A")
                    recruiter = app.get("recruiter", {}).get("name", "N/A")
                    stale.append((days_stale, name, job_name, stage, recruiter, app["id"]))

        if not stale:
            return "No stale applications found (all have had activity in the last 7 days)."

        stale.sort(reverse=True)
        lines = [f"## Stale Pipeline Report\n_{len(stale)} applications with no activity in 7+ days_\n"]
        for days, name, job, stage, recruiter, app_id in stale:
            lines.append(
                f"- **{name}** | {job} | Stage: {stage} | Recruiter: {recruiter} | "
                f"**{days} days since last activity** | App ID: {app_id}"
            )

        return "\n".join(lines)

    except Exception as e:
        return _handle_error(e)


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(
            mcp.streamable_http_app(),
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 8000)),
        )
