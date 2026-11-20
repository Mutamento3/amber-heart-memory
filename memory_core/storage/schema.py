"""Supported storage versions; migrations are ordered and immutable once applied."""

LATEST_VERSION = 31
GRAPH_VERSION = 3
DRAFT_VERSION = 4
REVIEW_VERSION = 5
RECALL_VERSION = 6
TOPIC_DETAILS_VERSION = 7
ACQUAINTANCE_DATE_VERSION = 8
TOPIC_PRESET_VERSION = 9
CONVERSATION_VERSION = 10
HOSTED_CONTEXT_VERSION = 11
SUMMARY_VERSION = 12
DIARY_VERSION = 13
DAILY_EXTRACTION_VERSION = 14
CARD_MUTATION_VERSION = 15
DELAYED_APPROVAL_VERSION = 16
ARCHIVE_VERSION = 17
TOPIC_SUMMARY_VERSION = 18
IMAGE_DESCRIPTION_VERSION = 19
RECENT_NOTES_VERSION = 20
CORE_MEMORY_VERSION = 21
IMPORT_VERSION = 22
IMPORT_APPROVAL_VERSION = 23
IMPORT_PROGRESS_VERSION = 24
KELIVO_IMPORT_VERSION = 25
STRUCTURE_CLAIM_VERSION = 26
DEEPSEEK_IMPORT_VERSION = 27
IMPORT_LIFECYCLE_VERSION = 28
PERIOD_NOTES_SWITCH_VERSION = 29
MEMORY_PARTIAL_DATE_VERSION = 30
HOOK_VERSION = 31
SUPPORTED_VERSIONS = frozenset(range(1, LATEST_VERSION + 1))

MIGRATION_FILES = {
    2: "002_migration_registry.sql",
    3: "003_graph_and_space_policies.sql",
    4: "004_materials_and_drafts.sql",
    5: "005_review_transactions.sql",
    6: "006_graph_retrieval.sql",
    7: "007_topic_details_and_review_context.sql",
    8: "008_space_acquaintance_date.sql",
    9: "009_fixed_topic_presets.sql",
    10: "010_conversations.sql",
    11: "011_hosted_context.sql",
    12: "012_conversation_summaries.sql",
    13: "013_diaries_and_daily_tasks.sql",
    14: "014_daily_card_extraction.sql",
    15: "015_card_deletion.sql",
    16: "016_delayed_approval.sql",
    17: "017_card_archiving.sql",
    18: "018_topic_summaries.sql",
    19: "019_image_descriptions.sql",
    20: "020_recent_notes.sql",
    21: "021_core_memory.sql",
    22: "022_imports.sql",
    23: "023_import_approval.sql",
    24: "024_import_run_progress.sql",
    25: "025_kelivo_import.sql",
    26: "026_structure_claims.sql",
    27: "027_deepseek_import.sql",
    28: "028_import_lifecycle.sql",
    29: "029_period_notes_switch.sql",
    30: "030_memory_file_partial_dates.sql",
    31: "031_hook_turns.sql",
}
