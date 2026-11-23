"""Prove that every regression test can actually fail.

A regression test that passes both before and after its fix is worse than no
test: it reports coverage it does not have. Four such tests were written and
caught by hand during the audit remediation, each for a different reason -
a changed function signature, a hardcoded assumption, a fake that finished too
fast to observe a race, and a positional argument that bypassed the code under
test. Manual vigilance caught those four; it will not catch the fifth.

This script makes the check repeatable. For each batch it checks the parent of
the fix commit out into a throwaway git worktree, drops the CURRENT test file
into it, and runs it there. At least one test must fail, or the batch's tests
are not evidence of anything.

Uses a worktree rather than `git stash` or `git checkout` deliberately: both of
those mutate the working tree, and mis-sequencing them destroyed a batch of
uncommitted work once already. A worktree cannot touch your checkout.

Usage:
    python scripts/verify_regression_tests.py            # all batches
    python scripts/verify_regression_tests.py batch3     # one batch

Known limitation (Windows): git cannot always delete its own
.git/worktrees/<name> metadata directory - the handle can still be held when
each batch's cleanup runs `git worktree remove --force`, `git worktree prune`
and deletes that directory - so roughly one small stale directory can
accumulate per invocation. It is inert: `git worktree list` stays
correct and no git operation is affected. Clear it with:

    rm -rf .git/worktrees
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, test file, the commit that introduced the fix)
BATCHES = [
    ("batch1", "tests/test_batch1_fixes.py", "3b20ee2"),
    ("batch2", "tests/test_batch2_fixes.py", "2aca7d5"),
    ("batch3", "tests/test_batch3_fixes.py", "f38a403"),
    ("batch4", "tests/test_batch4_fixes.py", "1d48563"),
    ("batch5", "tests/test_batch5_fixes.py", "9bf03a7"),
    ("batch6", "tests/test_batch6_fixes.py", "3368a22"),
    ("batch7", "tests/test_batch7_fixes.py", "a46b23d"),
    # Everything after the numbered batches. Named for the defect rather than
    # the sitting, because "batch8" tells a later reader nothing.
    ("counts", "tests/test_contact_counts_are_totals.py", "e550bd2"),
    ("stranded", "tests/test_search_never_strands.py", "33032f8"),
    ("dry-run", "tests/test_dry_run_is_a_simulation.py", "6479219"),
    ("cohort", "tests/test_bounce_rate_cohort.py", "3806f6e"),
    ("settings", "tests/test_settings_reach_enforcement.py", "08cb8e9"),
    ("verify", "tests/test_verification_never_falls_through.py", "08cb8e9"),
    ("funnel", "tests/test_analytics_funnel.py", "08cb8e9"),
    ("stopword", "tests/test_reply_intent_stop.py", "08cb8e9"),
    ("ai-output", "tests/test_generated_copy_is_checked.py", "530c3b3"),
    ("spamwords", "tests/test_deliverability_linter.py", "862350d"),
    ("encoding", "tests/test_reply_encoding.py", "9258a50"),
    ("erasure", "tests/test_erasure_is_complete.py", "9258a50"),
    ("competitor", "tests/test_competitor_gate.py", "9258a50"),
    ("extraction", "tests/test_extraction_quality.py", "9258a50"),
    ("guards", "tests/test_send_path_guards.py", "9258a50"),
    # Earlier fixes, added when this script was extended to cover the whole
    # suite rather than the seven numbered batches it shipped with.
    ("audit-regr", "tests/test_audit_regressions.py", "bb3a8c7"),
    ("audit-trail", "tests/test_audit_trail.py", "a2daa1f"),
    ("delivery", "tests/test_email_delivery_fixes.py", "adb7c02"),
    ("extract", "tests/test_extraction_and_bounce.py", "d3f2d58"),
    ("hardening", "tests/test_hardening.py", "09e1fe6"),
    ("inbox-scan", "tests/test_imap_inbox_scan.py", "0b3b2c8"),
    ("unsub-token", "tests/test_unsubscribe_token.py", "0b3b2c8"),
    ("query-src", "tests/test_query_sources.py", "c216419"),
    ("clock", "tests/test_timestamp_consistency.py", "cce88a9"),
    ("feat-review", "tests/test_feature_review_findings.py", "4d7e284"),
    ("allowance", "tests/test_agent_improvements.py", "453cba9"),
    # Files that were NOT introduced by a fix - they shipped as ordinary unit
    # coverage - but later had pin-the-bug tests appended to them by a fix
    # commit. The commit that created the file has no "before"; the commit that
    # added the regression test does, so that is what they are pinned to.
    # Exempting them on their birth commit would have hidden real regression
    # coverage behind "no prior state", which is the same silence this script
    # exists to break.
    ("failed-count", "tests/test_dashboard.py", "1444fbb"),
    ("dsn-delay", "tests/test_imap.py", "fa9df09"),
    ("geo-gate", "tests/test_relevance.py", "2e54ad6"),
    # From the session that left them unclassified (so this script exited 1).
    ("directory", "tests/test_directory_leads_survive.py", "b0a969f"),
    ("rival-voice", "tests/test_prospect_is_not_mistaken_for_a_rival.py", "bf123e3"),
    ("vertical-lang", "tests/test_vertical_gate_reads_every_language.py", "bf123e3"),
    ("robots-open", "tests/test_robots_fails_open.py", "c507e1a"),
    # The 2026-09-29 code, legal and ops review: one fix per commit.
    ("startup", "tests/test_client_list_cannot_stop_startup.py", "f7b083b"),
    ("plain-links", "tests/test_plain_text_keeps_the_links.py", "bf81edc"),
    ("tracking", "tests/test_tracking_is_off_and_blind.py", "8de5ba1"),
    ("auth-paths", "tests/test_auth_breaker_guards_every_send_path.py", "38c3514"),
    (
        "closed",
        "tests/test_followups_and_retries_stop_for_closed_contacts.py",
        "04ad922",
    ),
    ("send-now", "tests/test_send_now_respects_closed_contacts.py", "48e5df0"),
    (
        "mark-client",
        "tests/test_marking_a_client_reaches_the_running_agent.py",
        "93aa758",
    ),
    ("ds-backoff", "tests/test_direct_send_sees_domain_backoff.py", "dc29929"),
    ("comp-write", "tests/test_competitor_verdict_write_is_contained.py", "eded512"),
    ("comp-qualify", "tests/test_named_competitor_costs_nothing.py", "ca79ca5"),
    ("ds-screens", "tests/test_direct_send_screens_the_address.py", "425ad14"),
    ("eu-mailboxes", "tests/test_personal_mailboxes_across_europe.py", "a56faf3"),
    ("requalify", "tests/test_qualification_is_not_repeated.py", "3e6cca5"),
    ("yes-no", "tests/test_llm_verdict_is_a_word.py", "af85aec"),
    ("url-path", "tests/test_url_path_is_not_a_claim.py", "0c1cd6b"),
    ("want-ads", "tests/test_want_ads_are_prospects.py", "1445a35"),
    ("retry-link", "tests/test_retry_links_to_its_own_email.py", "a641003"),
    ("dsn-status", "tests/test_dsn_bounce_marks_the_contact.py", "cae50ac"),
    ("site-name", "tests/test_empty_site_name_falls_through.py", "c396c47"),
    ("erase-all", "tests/test_erasure_reaches_every_table.py", "760228c"),
    ("export-all", "tests/test_export_covers_every_table.py", "2ad7a54"),
    ("unsub-page", "tests/test_unsubscribe_page_is_accurate.py", "8d6191c"),
    # Findings of the one review round over the commits above.
    ("retry-dup", "tests/test_retry_never_repeats_a_first_email.py", "1ac8dd5"),
    (
        "stored-screen",
        "tests/test_stored_personal_mailboxes_are_not_emailed.py",
        "ecee4b6",
    ),
    ("rival-hook", "tests/test_rival_marketing_is_not_a_want_ad.py", "745fa76"),
    ("footer-runtime", "tests/test_template_without_footer_is_not_sent.py", "5a1b420"),
    ("whole-address", "tests/test_erasure_matches_whole_addresses.py", "e06c302"),
    # Review of the stage extraction (refactor/modular-agent).
    ("stop-trace", "tests/test_stop_is_recorded.py", "181943f"),
    ("send-gap", "tests/test_send_delay_cannot_be_negative.py", "6650cf5"),
    # The shared send check (Phase 3 of the stage-extraction review).
    ("backoff-swap", "tests/test_domain_backoff_is_swapped_whole.py", "c01e4af"),
    ("retry-order", "tests/test_retries_settle_before_the_domain_cap.py", "b3d8a98"),
    ("warmup-once", "tests/test_warmup_cap_is_logged_once.py", "90f5f4c"),
    ("sender-last", "tests/test_sender_is_picked_last.py", "e88c39e"),
    ("reread", "tests/test_loops_reread_each_contact.py", "aa3fb83"),
    ("first-once", "tests/test_campaign_never_repeats_a_first_email.py", "61ac213"),
    ("screens-first", "tests/test_screens_run_before_the_send.py", "cef76e9"),
    ("retry-requeue", "tests/test_failed_retry_returns_to_the_queue.py", "740f9b3"),
    ("ds-verdict", "tests/test_direct_send_uses_the_send_check.py", "2a436fb"),
    ("ds-closed", "tests/test_direct_send_refuses_closed_contacts.py", "78f1111"),
    ("ds-latch", "tests/test_direct_send_pauses_a_running_campaign.py", "a111b46"),
    ("no-sleep", "tests/test_no_sleep_after_a_refusal.py", "2345e39"),
    (
        "send-now-sent",
        "tests/test_send_now_refuses_an_already_emailed_contact.py",
        "57a3032",
    ),
    # Send-path performance (Phase 4).
    ("lower-index", "tests/test_address_lookups_use_an_index.py", "7bfa0ff"),
    (
        "qualify-gate",
        "tests/test_backlog_is_qualified_only_when_a_send_can_follow.py",
        "33793e9",
    ),
    (
        "retention-lock",
        "tests/test_retention_does_not_lock_out_the_dashboard.py",
        "b7afdb0",
    ),
    ("retry-once", "tests/test_retries_run_once_per_loop.py", "b873078"),
    # The "why" view, and the defects its read-only map found (Phase 5).
    ("preview", "tests/test_direct_send_preview_leaves_nothing_behind.py", "767d419"),
    ("optout-block", "tests/test_an_opt_out_cannot_be_unblocked.py", "9b09167"),
    ("conn-drop", "tests/test_a_dropped_connection_is_not_a_send.py", "eab98fa"),
    ("retry-write", "tests/test_a_failed_retry_queue_write_is_contained.py", "c32790b"),
    (
        "sent-log",
        "tests/test_a_delivered_send_survives_a_locked_activity_log.py",
        "2ad7322",
    ),
    ("cancel-reason", "tests/test_cancelled_follow_ups_say_why.py", "4be47bf"),
    ("send-reason", "tests/test_failed_sends_say_why.py", "2fe85da"),
    ("dash-actions", "tests/test_dashboard_actions_say_why.py", "371291f"),
    ("reply", "tests/test_a_reply_is_recorded_without_undoing_a_closure.py", "3956712"),
    # The review round's two findings (Phase 7).
    ("why-localtime", "tests/test_why_timeline_is_in_local_time.py", "bc9071c"),
    ("bounce-block", "tests/test_a_bounce_outlives_a_block.py", "0c04858"),
    ("retry-stats", "tests/test_retry_stats_count_cancellations.py", "6ebff58"),
    (
        "client-score",
        "tests/test_a_low_scoring_client_is_recorded_as_a_client.py",
        "8c4423d",
    ),
]

# Test files with no meaningful "before" to run against, so this script cannot
# say anything useful about them. Listing them explicitly is the point: a file
# in tests/ that is neither here nor in BATCHES gets reported as unverified,
# instead of being silently skipped the way fourteen of them were.
#
# FEATURE tests are exempt because failing at commit~1 proves nothing - the
# feature simply did not exist yet, so the test errors on an import rather than
# detecting a bug. Only a fix has a "before" where the code is present and
# wrong, which is the state this script needs.
NOT_REGRESSION_TESTS = {
    # Shipped with the initial commit - no prior state at all.
    "tests/test_ab_testing.py",
    "tests/test_agent_backlog.py",
    "tests/test_ai_generator.py",
    "tests/test_deliverability.py",
    "tests/test_email_render.py",
    "tests/ui/test_dashboard_ui.py",
    # Also shipped with the initial commit (ba8c589), and never touched since by
    # a commit that added a pin-the-bug test. Ordinary unit coverage of
    # behaviour that already worked: validator bounds, dedup and suppression,
    # the scoring dimensions, query variation, sequence idempotence.
    "tests/test_config.py",
    "tests/test_database.py",
    "tests/test_lead_scoring.py",
    "tests/test_query_generator.py",
    "tests/test_sequences.py",
    # Introduced alongside the feature they cover.
    "tests/test_attachment.py",
    "tests/test_lead_alerts.py",
    "tests/test_learning.py",
    "tests/test_manual_send.py",
    "tests/test_multi_account.py",
    "tests/test_timezone.py",
    "tests/test_v3_guards.py",
    # Feature tests whose file later gained a genuine regression test, but which
    # this method still cannot judge: the pre-fix module does not export the
    # name the file imports at module scope, so pytest dies in collection and
    # reports an error, not a failure. An ImportError is not evidence that a
    # test detects a bug - it is the "feature did not exist yet" case above,
    # arriving one commit later. Both were mutation-checked by hand instead.
    #
    #   test_blacklist.py  (feature 4001066; 0b3b2c8 added the PARTIAL-coverage
    #     regression) - `from modules.blacklist import PARTIAL` fails at
    #     0b3b2c8~1. Reverting the summarize() change fails that one test.
    #   test_learning_engine.py (7e5b6bc, the bandit rewrite) - MIN_WEIGHT and
    #     MAX_WEIGHT do not exist at 7e5b6bc~1. Its commit message records the
    #     mutations: substring matching fails 1 test, the old ratio fails 5, an
    #     advisory avoid list fails 1.
    "tests/test_blacklist.py",
    "tests/test_learning_engine.py",
    # Same ImportError shape, and these three were the reason this script exited
    # 1 on EVERY run - which is worse than useless, because a check that is
    # always red teaches people to ignore its exit code, and then it cannot
    # report the day something is genuinely wrong. They are real regression
    # tests; this METHOD simply cannot judge them, so they are covered by
    # mutation instead (scripts/mutation_audit.py), which needs no "before":
    #
    #   test_client_filter.py    @43b35f3 - ModuleNotFoundError: modules.clients
    #     did not exist yet.        Mutations CLI1-CLI3.
    #   test_review_findings.py  @8119e23 - ImportError: _has_term.
    #                                       Mutations REL2, VER1.
    #   test_runtime_config.py   @a8412bf - ImportError: RUNTIME_EDITABLE.
    #                                       Mutations CFG1-CFG3, added for this.
    "tests/test_client_filter.py",
    "tests/test_review_findings.py",
    "tests/test_runtime_config.py",
    # Written 2026-08-04 (36f7bf4) as NEW coverage for code that already existed
    # and had none, so there is no fix commit to run them against - the module
    # is byte-identical either side of the commit that added them, and this
    # script would report "0 fail" for the uninteresting reason. Their evidence
    # is mutation, recorded in 36f7bf4: moving the tier A boundary 75->70 and B
    # 50->40 previously survived the entire suite and now do not, and
    # advance_enrollment had no caller in any test - both other files that touch
    # the follow-up path monkeypatch it away - so an engine that never advanced
    # would have shipped green. Mutations were run in a COPY of the repo, never
    # the working tree. Both pin behaviour whose absence is silent: a mis-tiered
    # contact still gets emailed, a stuck enrollment still sends.
    "tests/test_lead_tiers.py",
    "tests/test_sequence_advances.py",
    # Same shape, from f7850e8: tests written to catch the five mutations that
    # SURVIVED scripts/mutation_audit.py. A surviving mutation means the guard
    # was present and CORRECT and merely unwatched - so the production code is
    # unchanged either side of f7850e8 (bar a warmup refactor that collapsed two
    # copies of one formula), and all four of these pass against f7850e8~1.
    # Measured, not assumed: 4/3/4/4 passed, 0 failed. That "0 fail" is the
    # benign reading - there was no shipped bug to detect - and the commit is
    # what says so. Their evidence is the mutation each was written against:
    # SUP2 (suppression stored with source casing), WRM2 (warm-up ramp with the
    # max_daily cap removed), RTY2 (an in_flight retry not counted as open).
    # test_warmup_status_matches_the_sender pins the same commit's warm-up
    # refactor, and no mutation runs it.
    "tests/test_suppression_is_case_insensitive.py",
    "tests/test_warmup_ceiling_holds.py",
    "tests/test_warmup_status_matches_the_sender.py",
    "tests/test_one_open_retry_per_contact.py",
    # Covers guards that ALREADY EXISTED when its commit added the tests, so
    # passing against commit~1 is the correct result, not a toothless one. Its
    # own docstring says as much: "None of these guards were covered". Listed
    # here after this script flagged it, because "9 tests, 0 fail" is the same
    # output for "cannot detect its bug" and "there was no bug to detect" - and
    # only reading the commit distinguishes them.
    "tests/test_followup_guards.py",
    # Regression tests, but browser tests: this script runs `pytest <file>`
    # without -m ui, so they are deselected and the run reports nothing. Both
    # were mutation-checked by hand when written - the XSS pair was confirmed
    # to fail against the pre-fix markup before the fix was applied.
    "tests/ui/test_row_actions_are_not_script.py",
    # Written 2026-08-05 (7288dd9) as NEW coverage for the existing send path,
    # driven against a real SMTP server that tests/smtp_sink.py stands up on
    # localhost. The commit changed no production code at all - it is tests
    # only - so modules/ is byte-identical either side of it and "0 fail"
    # against 7288dd9~1 would carry no information. Its evidence is that the
    # sink is a real server speaking real SMTP: the assertions are about what
    # the agent does with a genuine 550, a 4xx, a mid-message disconnect and a
    # rejected password, none of which a stub could have contradicted.
    "tests/test_send_path_against_real_smtp.py",
    # Written 2026-08-05 for three scraping capabilities that did not exist
    # before their commits, so running them against commit~1 fails on
    # AttributeError - the method under test is simply absent. That is the
    # "feature did not exist yet" case already described above, and a failure
    # for that reason is not evidence a test can detect a bug. Their evidence
    # is mutation instead, run against a COPY of the repo, never the working
    # tree, and recorded in each commit:
    #
    #   test_robots_compliance.py   (1f86cf5) - 4 mutations: unwiring the guard
    #     from _scrape_page, dropping the HTTP-status check, failing closed
    #     instead of open, and disabling the per-host cache. Note two of these
    #     tests originally could NOT fail - they asserted a 404/500 with an
    #     inert body, which parses as an empty ruleset and allows everything -
    #     and were rewritten to serve a body that WOULD disallow.
    #   test_contact_page_crawl.py  (5c001ac, 47ef6a5) - 8 mutations: both
    #     halves of guard 1 (organisation and context must come from the
    #     landing page), guard 2 (the address must be the company's own),
    #     guard 3 (one address per company), the homepage short-circuit, the
    #     robots check on the hop, and the brand-label comparison.
    #   test_browser_fetch.py       (86f5b47) - 5 mutations: dropping the
    #     curl_cffi->httpx exception translation, raise_for_status no longer
    #     raising, the body returned as str instead of bytes, impersonation
    #     never built, and the httpx fallback. The last of those needed a test
    #     added: never building the impersonating fetcher turned three tests
    #     into SKIPS rather than failures, so a silent downgrade kept the
    #     suite green.
    # Same shape, from the PEC fix: certified-mail filtering and brand-label
    # grouping did not exist before, so commit~1 fails on AttributeError.
    # Evidence is 3 mutations - unwiring the PEC filter, degrading its label
    # match to a substring match (which discards concert.it, specialita.it and
    # pecorino-shop.it, all real Italian prospects), and reverting guard 3 to
    # full-domain grouping.
    # Threading (Message-ID / In-Reply-To / References) did not exist before
    # its commit - no header was set anywhere - so commit~1 fails on the
    # missing kwarg rather than on detecting a bug. Evidence is 5 mutations:
    # never setting a Message-ID, letting a blank thread id through as a
    # malformed In-Reply-To, never storing the id, letting an UNSENT row start
    # a thread (pointing at mail the prospect never received), and the agent
    # never passing the id at all.
    # Guards the shipped .env.example against drifting from config.py's vetted
    # defaults. Not a fix for a shipped bug in the running agent - the live
    # config used the correct default - but a trap for every FRESH install,
    # since README.md and docs/DEPLOY.md both say "copy .env.example .env". Evidence
    # is its own mutation: putting info/sales back into the template fails
    # both tests.
    # A new screen, not a fix to a shipped bug: the named-competitor list did
    # not exist before its commit, so commit~1 fails on the missing attribute.
    # Evidence is 3 mutations - removing the send-time check, making matching
    # always miss, and failing closed instead of open.
    "tests/test_competitor_list.py",
    "tests/test_env_template_matches_defaults.py",
    "tests/test_followups_thread.py",
    "tests/test_certified_mail_is_not_marketing.py",
    "tests/test_robots_compliance.py",
    "tests/test_contact_page_crawl.py",
    "tests/test_browser_fetch.py",
    # Same shape, from de74910: tests written to kill two mutations that had
    # SURVIVED, once scripts/mutation_spec.py was extended past the modules/
    # boundary to agent.py and main.py. A surviving mutation means the guard
    # was present and CORRECT and merely unwatched, so that commit also changed
    # no production code and these pass against de74910~1 for the benign
    # reason. Their evidence is the mutation each was written against:
    # _process_contact's suppression check (deletable while green, because the
    # three files that appeared to cover it all stubbed _process_contact out),
    # and _clamp_limit (?limit=-1 reaching SQLite as LIMIT -1, i.e. unlimited,
    # dumping the whole prospect database in one response).
    "tests/test_chokepoint_and_limits.py",
    # 7dee54d changed tests and mutation_spec only - no production code - so
    # this passes against 7dee54d~1 for the benign reason. Written against
    # the two coverage gaps the mutation audit had flagged.
    "tests/test_chart_uses_the_app_clock.py",
    # New features from the 2026-09-29 review, not fixes: the legal footer,
    # the live-send refusal, the template editor's footer guard, retention
    # and the doctor checks did not exist before their commits, so commit~1
    # fails on a missing module or attribute. Their evidence is mutation -
    # groups legal, retention and doctor in scripts/mutation_spec.py.
    "tests/test_legal_footer.py",
    "tests/test_live_send_needs_a_legal_identity.py",
    "tests/test_template_editor_guards_the_footer.py",
    "tests/test_retention.py",
    "tests/test_doctor_checks.py",
    # A feature, not a fix: re-reading the client and competitor lists when
    # they change did not exist before c11cc20, so commit~1 fails on a
    # missing method. Evidence is mutation group `lists` (LST1-LST6).
    "tests/test_lists_reload_when_edited.py",
    # Coverage written for the stage extraction (e83dadd moved the code,
    # bc83428 added these). The modules are byte-identical either side of
    # bc83428, so "0 fail" against its parent is the benign reading. Several
    # of these originally could not fail at all; they were rewritten, and
    # the evidence is mutation group `stages` (STG1-STG6) plus every group
    # re-pointed at the stage modules - except test_stage_error_recovery.py,
    # which no mutation runs.
    "tests/test_exclusion_manager.py",
    "tests/test_dispatch_pipeline.py",
    "tests/test_backlog_draining.py",
    "tests/test_loop_orchestration.py",
    "tests/test_stage_error_recovery.py",
    # Tests the mutation runner itself. Its parent commit lacks `state_of`,
    # so it fails at import - the "did not exist yet" case. The defects it
    # pins (exit 5 and exit 2 read as CAUGHT, a non-unique anchor applied
    # to its first match) were reproduced against the old runner before the
    # fix; see commit d9b93f7.
    "tests/test_mutation_audit_reports_honestly.py",
    # Routing every stage-to-stage call through the agent is a consistency
    # fix with no user-visible defect behind it; its evidence is mutation
    # group `facade` (FAC1-FAC3), each caught.
    "tests/test_facade_routes_every_call.py",
    # The "why wasn't this contact emailed?" explainer is a new feature with
    # no prior state to fail against; its evidence is mutation group
    # `explainer`.
    "tests/test_contact_explainer.py",
    "tests/test_contact_why_endpoint.py",
    # The dashboard panel for it; evidence is mutation group `ui` (UI1-UI6,
    # run with marker "ui").
    "tests/ui/test_why_panel.py",
    # Characterises gdpr_export before records_held_about was pulled out of
    # it (7fdb581, no behaviour change); at the parent the new name does not
    # exist. Evidence is GDP7 and GDP9, which mutate the moved code and run it.
    "tests/test_records_held_about_one_address.py",
}


def report_unverified() -> bool:
    """Name every test file nobody has proved can fail.

    This script existed and went stale: it stopped at batch7 while fourteen
    later test files were added, and because nothing complained, the same
    mutation harness was rebuilt from scratch four times instead. Staleness
    that says nothing is the failure mode this whole codebase keeps hitting.
    """
    known = {b[1] for b in BATCHES} | NOT_REGRESSION_TESTS
    found = sorted(
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "tests").rglob("test_*.py")
    )
    missing = [f for f in found if f not in known]
    if missing:
        print("\nUNVERIFIED - no proof these can fail, and not marked exempt:")
        for f in missing:
            print(f"  {f}")
        print(
            "\nAdd each to BATCHES with the commit that introduced its fix, or "
            "to NOT_REGRESSION_TESTS if it guards behaviour rather than a fix."
        )
    return not missing


def _run(args, cwd=None):
    return subprocess.run(
        args,
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,  # a failing pytest run is the expected outcome here
    )


def _counts(output: str) -> tuple[int, int, bool]:
    """Extract (passed, failed, parsed) from pytest's summary line.

    `parsed` distinguishes "ran and nothing failed" from "never ran" - without
    it a collection error looks identical to a passing suite, which is the same
    class of false signal this whole script exists to catch. It reported
    "0 passed / 0 failed" as a definitive verdict on its first run.
    """
    passed = failed = 0
    parsed = False
    if m := re.search(r"(\d+) passed", output):
        passed, parsed = int(m.group(1)), True
    if m := re.search(r"(\d+) failed", output):
        failed, parsed = int(m.group(1)), True
    # Fallback: count the FAILED lines directly. pytest.ini sets addopts = -q,
    # so an extra -q makes it double-quiet and drops the summary line entirely -
    # which is exactly how this parser broke the first time.
    explicit = len(re.findall(r"^FAILED ", output, re.MULTILINE))
    if explicit and not parsed:
        failed, parsed = explicit, True
    elif explicit > failed:
        failed = explicit
    return passed, failed, parsed


def verify(label: str, test_file: str, fix_commit: str) -> bool:
    src = ROOT / test_file
    if not src.exists():
        print(f"  {label:8} SKIP  {test_file} not found")
        return True

    tmp = Path(tempfile.mkdtemp(prefix=f"verify-{label}-"))
    worktree = tmp / "wt"
    added = False
    try:
        r = _run(
            ["git", "worktree", "add", "--detach", str(worktree), f"{fix_commit}~1"]
        )
        if r.returncode != 0:
            print(f"  {label:8} ERROR worktree failed: {r.stderr.strip()[:90]}")
            return False
        added = True

        # The current test file, run against the pre-fix source.
        dest = worktree / test_file
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

        r = _run([sys.executable, "-m", "pytest", test_file], cwd=worktree)
        output = r.stdout + r.stderr
        passed, failed, parsed = _counts(output)

        if not parsed:
            print(f"  {label:8} ERROR pytest produced no result in the worktree:")
            for line in output.strip().splitlines()[-6:]:
                print(f"           | {line[:100]}")
            return False

        if failed == 0:
            print(
                f"  {label:8} FAIL  {passed} passed / 0 failed against {fix_commit}~1 "
                "- these tests cannot detect the bug they document"
            )
            return False
        print(
            f"  {label:8} OK    {failed} discriminating / {passed} guards "
            f"(vs {fix_commit}~1)"
        )
        return True
    finally:
        if added:
            _run(["git", "worktree", "remove", "--force", str(worktree)])
            # On Windows git often cannot delete its own metadata directory
            # while the handle is open; prune, then remove what is left so
            # .git/worktrees does not accumulate stale entries.
            _run(["git", "worktree", "prune"])
            stale = ROOT / ".git" / "worktrees" / worktree.name
            shutil.rmtree(stale, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    batches = [b for b in BATCHES if not wanted or b[0] == wanted]
    if not batches:
        print(f"no batch named {wanted!r}; known: {', '.join(b[0] for b in BATCHES)}")
        return 2

    print("Verifying regression tests fail against their pre-fix code\n")
    # Every batch is run before the verdict is taken. all(...) over a generator
    # short-circuits on the first failure and silently skips the rest, so one
    # broken batch would hide every later one - the same "absence reported as a
    # clean result" shape this script exists to catch.
    results = [verify(*b) for b in batches]
    ok = all(results)
    complete = report_unverified() if not wanted else True
    print()
    if ok and complete:
        print("All batches discriminate.")
    elif not ok:
        print("Some tests prove nothing - see above.")
    else:
        print("Batches pass, but some test files have never been verified.")
    return 0 if (ok and complete) else 1


if __name__ == "__main__":
    raise SystemExit(main())
