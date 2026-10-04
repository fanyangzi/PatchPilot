from ..domain import FailureClass
RECOVERY={
 FailureClass.TEST_ASSERTION:['rollback','inspect_related_tests','rebuild_context','retry_patch'],
 FailureClass.REGRESSION:['rollback','run_regression_subset','retry_patch'],
 FailureClass.DEPENDENCY_CONFLICT:['lock_dependency','rebuild_environment','retry_patch'],
 FailureClass.PATCH_CONFLICT:['reset_worktree','rebase_context','retry_patch'],
 FailureClass.TIMEOUT:['reduce_scope','increase_observability','retry_once'],
 FailureClass.LICENSE_OR_RISK:['block_delivery','request_human_review'],
}
def actions_for(failure): return RECOVERY.get(failure,['collect_trace','request_human_review'])
