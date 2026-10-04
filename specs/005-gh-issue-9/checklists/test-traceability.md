# Test Traceability: CLI Job Management (gh-issue-9)

Maps every functional requirement, acceptance scenario and edge case in [spec.md](../spec.md),
and every exit code and message in [contracts/cli-job.md](../contracts/cli-job.md), to the tests
that cover it. Tests marked **(QA)** were added during the QA pass. Tests marked **(QA, regression)**
reproduce a bug that was fixed in `src/` during that pass.

File abbreviations (all under `tests/`):

| Abbr | File |
|---|---|
| C | `test_cli_job.py` (help, list, show, missing setting) |
| CR | `test_cli_job_create.py` (interactive create via CliRunner) |
| E | `test_cli_job_edit.py` (edit, fake editor) |
| M | `test_cli_job_manage.py` (create --from-file, enable/disable/delete, export/import, seams) |
| W | `test_wizard.py` (wizard unit tests, scripted prompter) |
| SC | `test_source_check.py` (HTTP checker against a local server) |
| ED | `test_editor.py` (editor launcher) |
| L | `test_cli_layering.py` (import layering, prompt adapters) |
| JS | `test_job_service.py` (service layer used by the CLI) |

## Functional requirements

| FR | Requirement (short) | Tests |
|---|---|---|
| FR-001 | `invio job` group with 9 sub-commands, no `scout` | C::test_help_lists_all_nine_commands |
| FR-002 | stdout/stderr split, 0/non-zero/2 exit codes | M::test_expected_failures_never_print_a_traceback; M::test_unknown_job_exits_1 (stdout empty, QA); C::test_every_command_without_database_setting_exits_2 (QA, regression); C::test_list_empty_writes_nothing_to_stderr (QA); C::test_show_success_writes_nothing_to_stderr (QA); C::test_show_invalid_stored_job_keeps_errors_off_stdout (QA); E::test_invalid_then_abort_is_byte_identical (QA); M::test_import_invalid_file_reports_fields_on_stderr (QA); CR::test_declined_preview (stderr, QA-strengthened); M::test_delete_declined (stderr, QA-strengthened) |
| FR-003 | No second job config definition | L::test_cli_does_not_import_the_database_layer; C::test_show (body == `dump_yaml(validate_job)`); CR::test_preview_is_exactly_the_export (QA) |
| FR-004 | Wizard question order | W::test_questions_in_documented_order; W::test_daily_asks_neither_weekday_nor_day; W::test_monthly_asks_day_not_weekday |
| FR-005 | Inline validation, re-ask, keep earlier answers | W::test_inline_rejections; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[name,time,timezone,email,url] (QA); W::test_going_back_to_the_step_that_owns_the_error |
| FR-006 | E-mail syntax, at least one recipient | W::test_inline_rejections[not-an-email]; W::test_recipients_first_required_and_duplicates_skipped; CR::test_bad_email_then_good_email |
| FR-007 | URL syntax (absolute http/https) | W::test_inline_rejections[htp:/example]; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[url] (QA); SC::test_unsupported_urls_do_not_raise |
| FR-008 | Reachability with timeout, HEAD then GET, warn + override | SC::test_head_not_allowed_falls_back_to_get; SC::test_slow_server_times_out; SC::test_total_deadline_stops_slow_drip; SC::test_not_found; SC::test_connection_refused_does_not_raise; W::test_unreachable_source_keep_declined_asks_url_again; W::test_unreachable_source_kept_when_confirmed; CR::test_unreachable_url_override[override,re-enter] (QA) |
| FR-009 | RSS feed detection with override | SC::test_feed_documents; SC::test_non_feed_documents; SC::test_feed_detected; SC::test_html_page_is_not_a_feed; W::test_rss_without_feed_warns; CR::test_rss_feed_detection_warning[override,re-enter] (QA) |
| FR-010 | YouTube ids instead of URLs, no check | W::test_youtube_sources_ask_ids_and_skip_checks |
| FR-011 | ≥1 source, non-empty description, keywords optional | W::test_empty_description_is_rejected; W::test_keywords_blank_items_dropped_and_empty_allowed; W::test_duplicate_source_is_skipped_with_warning (loop needs ≥1 by construction) |
| FR-012 | Providers, registry models, Other… + confirm, limits | W::test_model_choices_come_from_registry_plus_other; W::test_other_unknown_model_warns_and_confirms; W::test_other_unknown_model_declined_asks_again; W::test_provider_without_models_gives_free_text_with_warning; W::test_registry_none_gives_free_text_without_warning; W::test_broken_registry_warns_once_and_uses_free_text; W::test_limits_default_yes_asks_nothing_more; W::test_limits_no_asks_four_values_prefilled; CR::test_unknown_model_warning_goes_to_stderr (QA) |
| FR-013 | YAML preview == export, confirm, decline saves nothing | W::test_validate_job_result_is_what_the_preview_shows; W::test_declined_preview_returns_none; CR::test_declined_preview; CR::test_preview_is_exactly_the_export (QA) |
| FR-014 | Full validation before preview, back to owning step | W::test_going_back_to_the_step_that_owns_the_error; W::test_unattributable_error_propagates; W::test_no_owning_step_for_empty_error_list |
| FR-015 | Abort (Ctrl+C/EOF) saves nothing, non-zero, no traceback | CR::test_abort_leaves_nothing_and_no_traceback; CR::test_ctrl_c_during_source_check; CR::test_ctrl_c_at_any_step_saves_nothing[11 steps × KeyboardInterrupt/EOFError/WizardAborted] (QA); W::test_running_out_of_answers_aborts; L::test_questionary_none_answer_aborts |
| FR-016 | No TTY: fail fast, point to --from-file | CR::test_no_terminal_exits_2_without_building_a_prompter; M::test_is_interactive_needs_both_streams_to_be_terminals |
| FR-017 | `create --from-file`, no prompts, no TTY | M::test_create_from_file (forbid_prompts, non-interactive) |
| FR-018 | Name = file stem or `--name` | M::test_create_from_file; M::test_create_from_file_with_name |
| FR-019 | No network checks; field-named errors | M::test_create_from_file (checker forbidden); M::test_create_from_invalid_file |
| FR-020 | List table columns, sorted by name | C::test_list_rows; C::test_list_broken_job_next_to_valid_jobs (QA) |
| FR-021 | Human-readable next run with zone | C::test_fmt_next_run; C::test_list_rows; C::test_show |
| FR-022 | Last status = newest run or "never run" | C::test_list_rows; C::test_list_shows_last_run_status; C::test_list_shows_every_run_status[running,succeeded,partial,failed] (QA) |
| FR-023 | `show` prints YAML, enabled, next run | C::test_show; C::test_show_success_writes_nothing_to_stderr (QA) |
| FR-024 | `$VISUAL`/`$EDITOR`, platform fallback | ED::test_visual_wins_over_editor; ED::test_fallback_editor_per_platform; ED::test_returns_edited_text; E::test_default_edit_seam_delegates_to_editor |
| FR-025 | Validate after save; retry with edited text or abort | E::test_valid_change; E::test_invalid_then_retry_then_valid; E::test_invalid_then_abort; E::test_retry_then_close_without_saving_does_not_succeed (QA, regression); E::test_validation_errors_name_the_field (QA); E::test_schedule_change_recalculates_next_run (QA) |
| FR-026 | Abort / unchanged leaves job unchanged | E::test_no_changes; E::test_invalid_then_abort; E::test_invalid_then_abort_is_byte_identical (QA); E::test_editor_failure_leaves_job_unchanged; E::test_retry_then_editor_failure_leaves_job_unchanged (QA) |
| FR-027 | No implicit rename | E::test_name_key_is_rejected_and_name_unchanged; E::test_invalid_then_abort_is_byte_identical[rename-attempt] (QA) |
| FR-028 | enable/disable report state; repeat is a no-op | M::test_disable_then_enable; M::test_repeated_enable_and_disable_are_noops; M::test_repeated_enable_and_disable_change_nothing (QA); M::test_enable_after_disable_computes_a_fresh_next_run (QA) |
| FR-029 | delete confirm / `--yes` / no-TTY refusal | M::test_delete_confirmed; M::test_delete_declined; M::test_delete_declined_leaves_job_byte_identical (QA); M::test_delete_yes_without_terminal; M::test_delete_without_terminal_refuses |
| FR-030 | export to stdout or file | M::test_export_to_stdout; M::test_export_to_file_round_trips; M::test_export_to_file_does_not_print_yaml (QA); M::test_export_write_failure |
| FR-031 | import: stem/name, refuse overwrite unless `--replace`, field errors | M::test_import_flow; M::test_import_replace_of_new_name_says_imported; M::test_import_existing_name_leaves_the_job_untouched (QA); M::test_import_invalid_file; M::test_import_invalid_file_reports_fields_on_stderr (QA); M::test_import_bad_name; M::test_import_without_terminal_never_prompts (QA) |
| FR-032 | list shows invalid config | C::test_list_invalid_config; C::test_list_broken_job_next_to_valid_jobs (QA); C::test_list_invalid_config_takes_precedence_over_run_status (QA); JS::test_overview_includes_invalid_config |
| FR-033 | show invalid: stored YAML + errors, exit 2 | C::test_show_invalid_stored_job; C::test_show_invalid_stored_job_keeps_errors_off_stdout (QA) |
| FR-034 | edit invalid: errors first, stored content, repair | E::test_broken_stored_job_is_repaired; E::test_broken_job_errors_shown_before_editor_opens (QA); E::test_no_changes_on_broken_job_keeps_it_broken (QA) |
| FR-035 | disable/delete work; enable/export refuse with exit 2 | M::test_disable_broken_job_warns_but_succeeds; M::test_delete_broken_job; M::test_export_broken_job; M::test_enable_broken_job_keeps_it_disabled (QA) |
| FR-036 | CliRunner tests, no TTY/network/editor | All CLI tests use `job_cli` fakes (`tests/cli_helpers.py`); SC uses a local loopback server; ED uses a scripted Python "editor" |

## Acceptance scenarios

| Scenario | Tests |
|---|---|
| US1-1 wizard → saved, success line, in list with next run | CR::test_full_run_creates_job_and_list_shows_it (QA-strengthened: exact line + list row regex) |
| US1-2 bad recipient re-asked, answers kept | CR::test_bad_email_then_good_email; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[email] (QA) |
| US1-3 duplicate / invalid name re-asked | W::test_inline_rejections[taken, whitespace, too long, empty]; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[name] (QA) |
| US1-4 weekly→weekday, monthly→day, daily→neither | W::test_daily_asks_neither_weekday_nor_day; W::test_monthly_asks_day_not_weekday; W::test_questions_in_documented_order |
| US1-5 `25:00` / `Mars/Olympus` rejected | W::test_inline_rejections; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[time,timezone] (QA) |
| US1-6 decline preview → nothing saved | CR::test_declined_preview |
| US1-7 Ctrl+C at any step | CR::test_ctrl_c_at_any_step_saves_nothing (QA); CR::test_abort_leaves_nothing_and_no_traceback; CR::test_ctrl_c_during_source_check |
| US2-1 malformed URL rejected | W::test_inline_rejections[htp:/example]; CR::test_inline_rejection_reasks_and_keeps_earlier_answers[url] (QA) |
| US2-2 unreachable → warn, keep or re-enter | W::test_unreachable_source_keep_declined_asks_url_again; W::test_unreachable_source_kept_when_confirmed; CR::test_unreachable_url_override (QA) |
| US2-3 rss not a feed → warn, keep or re-enter | W::test_rss_without_feed_warns; CR::test_rss_feed_detection_warning (QA) |
| US2-4 YouTube asks ids, no check | W::test_youtube_sources_ask_ids_and_skip_checks |
| US2-5 "add another?" loop, ≥1 source | W::test_youtube_sources_ask_ids_and_skip_checks; W::test_duplicate_source_is_skipped_with_warning |
| US3-1 list rows sorted | C::test_list_rows; C::test_list_broken_job_next_to_valid_jobs (QA) |
| US3-2 never run placeholder | C::test_list_rows |
| US3-3 no jobs → friendly message, exit 0 | C::test_list_empty; C::test_list_empty_writes_nothing_to_stderr (QA) |
| US3-4 show prints YAML + state + next run | C::test_show |
| US3-5 invalid stored config in list/show (exit 2) | C::test_list_invalid_config; C::test_show_invalid_stored_job |
| US3-6 unknown name → error, non-zero | M::test_unknown_job_exits_1 (6 commands) |
| US4-1 from file, no TTY, exit 0 | M::test_create_from_file |
| US4-2 invalid file → field errors, nothing stored, exit 2 | M::test_create_from_invalid_file |
| US4-3 existing name → already exists, untouched | M::test_create_from_file_existing_name |
| US4-4 no TTY, no file → fail fast | CR::test_no_terminal_exits_2_without_building_a_prompter |
| US5-1 valid edit updates, next run recalculated | E::test_valid_change; E::test_schedule_change_recalculates_next_run (QA) |
| US5-2 invalid → errors + retry with edited text | E::test_invalid_then_retry_then_valid; E::test_retry_then_close_without_saving_does_not_succeed (QA, regression) |
| US5-3 abort → unchanged, non-zero | E::test_invalid_then_abort; E::test_invalid_then_abort_is_byte_identical (QA) |
| US5-4 no changes → told, unchanged | E::test_no_changes |
| US5-5 no editor configured → platform default | ED::test_fallback_editor_per_platform |
| US5-6 broken stored job: errors, stored content, repair | E::test_broken_stored_job_is_repaired; E::test_broken_job_errors_shown_before_editor_opens (QA) |
| US6-1 disable → not scheduled; enable → fresh next run | M::test_disable_then_enable; M::test_enable_after_disable_computes_a_fresh_next_run (QA) |
| US6-2 repeat state → success, no change | M::test_repeated_enable_and_disable_are_noops; M::test_repeated_enable_and_disable_change_nothing (QA) |
| US6-3 delete confirmed | M::test_delete_confirmed; M::test_delete_removes_run_history (QA) |
| US6-4 delete declined | M::test_delete_declined; M::test_delete_declined_leaves_job_byte_identical (QA) |
| US6-5 `--yes` without TTY; refusal without | M::test_delete_yes_without_terminal; M::test_delete_without_terminal_refuses |
| US7-1 export stdout / file | M::test_export_to_stdout; M::test_export_to_file_round_trips |
| US7-2 import under stem or name | M::test_import_flow |
| US7-3 existing name: fail, or `--replace` updates | M::test_import_flow; M::test_import_existing_name_leaves_the_job_untouched (QA) |
| US7-4 invalid file → field errors, exit 2 | M::test_import_invalid_file; M::test_import_invalid_file_reports_fields_on_stderr (QA); M::test_import_replace_with_invalid_file_leaves_the_job_untouched (QA) |
| US7 independent test: export → delete → import equal | M::test_export_import_round_trip_gives_an_equal_config[stdout,file] (QA) |

## Edge cases

| Edge case | Tests |
|---|---|
| Timeout / network down → warning, bounded | SC::test_slow_server_times_out; SC::test_total_deadline_stops_slow_drip; SC::test_connection_refused_does_not_raise; SC::test_dns_failure_reason; CR::test_unreachable_url_override (QA) |
| Redirects followed | SC::test_redirect_is_followed |
| HEAD 405 → GET fallback | SC::test_head_not_allowed_falls_back_to_get |
| Monthly day 29–31 note | W::test_monthly_end_of_month_note; W::test_monthly_day_28_has_no_note |
| `--name` invalid / taken → exit 1 before first question | CR::test_invalid_name_option_is_rejected_before_the_wizard; CR::test_existing_name_option_is_rejected_before_the_wizard |
| Duplicate recipients / sources warned, not added twice | W::test_recipients_first_required_and_duplicates_skipped; W::test_duplicate_source_is_skipped_with_warning |
| Empty keywords allowed; empty description rejected | W::test_keywords_blank_items_dropped_and_empty_allowed; W::test_empty_description_is_rejected |
| Multi-line description preserved | W::test_multiline_description_keeps_newlines |
| Limits: defaults skip; non-int / < 1 rejected | W::test_limits_default_yes_asks_nothing_more; W::test_limit_rejections |
| Unregistered model: warn, confirm, decline re-asks | W::test_other_unknown_model_warns_and_confirms; W::test_other_unknown_model_declined_asks_again |
| Ctrl+C / EOF at any prompt | CR::test_ctrl_c_at_any_step_saves_nothing (QA); M::test_ctrl_c_and_eof_exit_1 |
| Editor exits non-zero → abort, unchanged | E::test_editor_failure_leaves_job_unchanged; E::test_retry_then_editor_failure_leaves_job_unchanged (QA); ED::test_nonzero_exit_is_error_and_cleans_up |
| Name taken between wizard start and save → YAML kept | CR::test_name_taken_after_the_wizard_keeps_the_preview |
| Stored config became invalid | FR-032 to FR-035 rows above; E::test_vanished_job_during_edit (job disappears during edit) |

## Contract: exit codes and messages (`contracts/cli-job.md`)

| Contract item | Tests |
|---|---|
| Missing `INVIO_DATABASE_URL` → `Configuration error: …`, exit 2 (every command) | C::test_every_command_without_database_setting_exits_2 (QA, regression); C::test_list_missing_database_setting |
| Exit 0: no-op enable/disable, unchanged edit | M::test_repeated_enable_and_disable_are_noops; E::test_no_changes |
| Exit 1: not found | M::test_unknown_job_exits_1 |
| Exit 1: already exists | M::test_create_from_file_existing_name; CR::test_existing_name_option_is_rejected_before_the_wizard; M::test_import_existing_name_leaves_the_job_untouched (QA) |
| Exit 1: invalid name `Error: invalid job name '…': …` | CR::test_invalid_name_option_is_rejected_before_the_wizard; M::test_import_bad_name |
| Exit 1: aborted `aborted; nothing saved` | CR::test_ctrl_c_at_any_step_saves_nothing (QA); CR::test_ctrl_c_during_source_check |
| Exit 1: `job not created` | CR::test_declined_preview |
| Exit 1: `job not deleted` | M::test_delete_declined |
| Exit 1: `edit aborted; job unchanged` | E::test_invalid_then_abort; E::test_editor_failure_leaves_job_unchanged |
| Exit 1: database error, no secrets | M::test_database_error_hides_details |
| Exit 1: export write failure | M::test_export_write_failure |
| Exit 2: `invalid job file …:` + indented field lines | M::test_create_from_invalid_file; M::test_import_invalid_file_reports_fields_on_stderr (QA); E::test_validation_errors_name_the_field (QA) |
| Exit 2: `invalid stored job '…':` (show/enable/export) | C::test_show_invalid_stored_job; M::test_export_broken_job; M::test_enable_broken_job_keeps_it_disabled (QA) |
| Exit 2: `interactive creation needs a terminal; use …--from-file…` | CR::test_no_terminal_exits_2_without_building_a_prompter |
| Exit 2: `refusing to delete without confirmation; pass --yes` | M::test_delete_without_terminal_refuses |
| `created job 'NAME' (next run: YYYY-MM-DD HH:MM TZ)` | CR::test_full_run_creates_job_and_list_shows_it; M::test_create_from_file |
| `no jobs yet — create one with 'invio job create'` | C::test_list_empty |
| list columns, `yes`/`no`, frequency text, `—` | C::test_list_rows; C::test_list_frequency_text; C::test_list_invalid_config |
| show header lines + blank line + YAML | C::test_show |
| `Re-open the editor? [Y/n]` | E::test_invalid_then_abort; E::test_retry_then_close_without_saving_does_not_succeed (QA) |
| `updated job 'NAME' (next run: …)` | E::test_valid_change |
| `enabled job …` / `already enabled` / `disabled job …` / `already disabled` | M::test_disable_then_enable; M::test_repeated_enable_and_disable_are_noops |
| Disable invalid config: exit 0 + stderr warning | M::test_disable_broken_job_warns_but_succeeds |
| `Delete job 'NAME' and its run history? [y/N]` | M::test_delete_confirmed; M::test_delete_declined_leaves_job_byte_identical (QA) |
| `deleted job 'NAME'` | M::test_delete_confirmed; M::test_delete_removes_run_history (QA) |
| `exported job 'NAME' to PATH` | M::test_export_to_file_round_trips; M::test_export_to_file_does_not_print_yaml (QA) |
| `imported job 'NAME'` / `replaced job 'NAME'` | M::test_import_flow; M::test_export_import_round_trip_gives_an_equal_config (QA) |
| Wizard transcript messages (`✗ …`, `! reachable, but no RSS/Atom feed detected`, `✓ feed detected`, unregistered model) | W::test_inline_rejections; CR::test_rss_feed_detection_warning (QA); W::test_feed_detected_message_goes_to_stdout; CR::test_unknown_model_warning_goes_to_stderr (QA) |

## Known untested surface

- `QuestionaryPrompter` methods (`# pragma: no cover`): a thin wrapper over a real terminal,
  checked manually per tasks T040. The `None` → `WizardAborted` mapping is unit-tested
  (L::test_questionary_none_answer_aborts).
- SC-001 (wizard in under 3 minutes) is a usability goal and is not automated.
