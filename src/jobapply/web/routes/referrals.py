"""Referral drafts review page.

Drafts are safe: generating or queuing them never contacts anyone. Approving a draft on this page
only marks it ready for a human to send later; it never sends. Sending a connection request or
message is a separate, gated, manual step (global referral send switch off by default, a per-user
daily cap, the kill switch, active hours, and per-draft approval) that lives in the apply worker /
CLI, not in this dashboard, because LinkedIn bans accounts for automated outreach.

The ``ReferralDraft`` model (table ``referral_drafts``) is built by another agent. It is imported
lazily inside the handlers so this router loads even before that model lands; when it is missing the
page renders empty with a short note instead of erroring.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from jobapply.models import Job, utcnow
from jobapply.web.deps import Ctx, flash, get_ctx, read_form

router = APIRouter()

# Status buckets, in the order shown on the page.
STATUS_ORDER = ("draft", "approved", "sent", "skipped", "failed")
STATUS_LABELS = {
    "draft": "Drafts",
    "approved": "Approved (not sent)",
    "sent": "Sent",
    "skipped": "Skipped",
    "failed": "Failed",
}


def _referral_model() -> Any | None:
    """The ReferralDraft model, or None when the other agent's model has not landed yet."""
    try:
        from jobapply.models import ReferralDraft  # type: ignore[attr-defined]
    except ImportError:
        return None
    return ReferralDraft


def _jobs_by_id(ctx: Ctx, job_ids: set[int]) -> dict[int, Job]:
    if not job_ids:
        return {}
    from sqlmodel import select

    rows = ctx.session.exec(select(Job).where(Job.id.in_(job_ids))).all()
    return {j.id: j for j in rows}


@router.get("/referrals")
def referrals_page(ctx: Ctx = Depends(get_ctx)):
    model = _referral_model()
    groups: dict[str, list[tuple[Any, Job | None]]] = {s: [] for s in STATUS_ORDER}
    other: list[tuple[Any, Job | None]] = []
    total = 0
    if model is not None:
        drafts = list(ctx.scope.all(model, order_by=model.created_at.desc()))
        jobs = _jobs_by_id(ctx, {d.job_id for d in drafts if getattr(d, "job_id", None)})
        for d in drafts:
            total += 1
            job = jobs.get(getattr(d, "job_id", None))
            bucket = groups.get(d.status)
            (bucket if bucket is not None else other).append((d, job))
    return ctx.render(
        "referrals.html",
        available=model is not None,
        groups=groups,
        other=other,
        total=total,
        status_order=STATUS_ORDER,
        status_labels=STATUS_LABELS,
    )


def _get_draft(ctx: Ctx, draft_id: int):
    model = _referral_model()
    if model is None:
        raise HTTPException(404)
    draft = ctx.scope.get(model, draft_id)
    if draft is None:
        raise HTTPException(404)
    return draft


@router.post("/referrals/{draft_id}/save")
async def referral_save(draft_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    form = await read_form(request)
    draft = _get_draft(ctx, draft_id)
    if "body" in form:
        draft.body = str(form["body"])
    draft.updated_at = utcnow()
    ctx.session.add(draft)
    ctx.session.commit()
    flash(request, "Saved. This only edits the draft; nothing was sent.")
    return RedirectResponse("/referrals", status_code=303)


@router.post("/referrals/{draft_id}/approve")
async def referral_approve(draft_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    # Approve does NOT touch the body: the approve form carries no body field, so it can never overwrite the
    # draft with a stale value. Edit then "Save edits" to change the text; this only changes the status.
    await read_form(request)
    draft = _get_draft(ctx, draft_id)
    if draft.status == "draft":
        draft.status = "approved"
        draft.updated_at = utcnow()
        ctx.session.add(draft)
        ctx.session.commit()
        flash(request, "Approved. Approving does NOT send — sending is a separate, manual, capped step "
                       "that is off by default.")
    else:
        flash(request, f"No change: draft is already {draft.status}.", "error")
    return RedirectResponse("/referrals", status_code=303)


@router.post("/referrals/{draft_id}/unapprove")
async def referral_unapprove(draft_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    draft = _get_draft(ctx, draft_id)
    if draft.status == "approved":
        draft.status = "draft"
        draft.updated_at = utcnow()
        ctx.session.add(draft)
        ctx.session.commit()
        flash(request, "Moved back to draft.")
    else:
        flash(request, f"No change: draft is {draft.status}, not approved.", "error")
    return RedirectResponse("/referrals", status_code=303)


@router.post("/referrals/{draft_id}/skip")
async def referral_skip(draft_id: int, request: Request, ctx: Ctx = Depends(get_ctx)):
    await read_form(request)
    draft = _get_draft(ctx, draft_id)
    if draft.status not in ("sent", "skipped"):
        draft.status = "skipped"
        draft.updated_at = utcnow()
        ctx.session.add(draft)
        ctx.session.commit()
        flash(request, "Skipped. It will not be sent.")
    else:
        flash(request, f"No change: draft is already {draft.status}.", "error")
    return RedirectResponse("/referrals", status_code=303)
