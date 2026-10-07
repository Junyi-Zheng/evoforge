# Experiment protocol

## Define the claim before running

A useful question is: under a fixed model and per-task inference allowance, does modifying the agent program improve success on unseen tasks, and what is the cost of finding that program?

The frozen seed provides the initial baseline. Report its validation score and inference cost alongside the selected program, total search cost, final-test outcome, and all failed candidate attempts. A single lucky candidate is insufficient evidence of a general method.

## Three information boundaries

| Split | Used during search | Visible to proposal |
| --- | --- | --- |
| Train | Diagnose failures and evaluate candidates | Task instructions, visible source, public feedback, execution traces, aggregate pass/fail |
| Validation | Parent selection, regression gates, champion selection | No traces, inputs, or expected values |
| Test | Evaluate the fixed champion after `seal` | Nothing |

Selection necessarily adapts to validation scores, so validation is not an unbiased final measurement. Final tests are withheld by the controller/container interface, not encrypted against the person operating the host. Avoid manually inspecting or tuning against the final split. A sealed run cannot evolve further. A retry of an interrupted final evaluation keeps the same candidate and charges all earlier work.

The bundled suite has 8 tasks per split, with layout variations and distinct randomized cases across four shared task families. A successful demo does not establish cross-domain transfer. Use a separate benchmark with independent task families or repository/time splits for that claim. The suite loader rejects exact duplicate task records, but cannot detect semantic overlap or model pretraining contamination.

## Hold controls fixed

Record a pinned model identifier, endpoint, suite, seed genome, controller revision, Docker image ID, repetitions, and per-task limits. The model adapter requests temperature zero; endpoint behavior can still be nondeterministic. Calls use a single model for both execution and proposal in this version.

The run seed controls parent sampling and generated fixture cases. For multiple search seeds on one fixed suite, export that suite once and pass the same JSON to all runs. Repetitions re-execute each task; they do not generate independent tasks. `compare` checks suite, controller, seed, runtime, model, and budget/selection controls, while allowing different search seeds.

## Metrics

- **Task success:** all public and private cases pass, required input preservation holds, and the action-time/call/token allowances are respected. Public feedback alone cannot mark a task as solved.
- **Inference cost:** mean reported/estimated model tokens per task. Mutation cost is excluded from this mean and included in total run cost.
- **Search cost:** all model requests, including invalid proposals, failures, abandoned evaluations, ablations, and final evaluation.
- **Time:** observed wall time includes external grading. The episode action loop has its own time budget; each grading invocation has a separate runtime deadline. Container startup contributes to observed time.
- **Pareto frontier:** maximize validation success and minimize mean inference tokens. Time is reported but is not used for ranking because process startup adds noise.
- **Regression gate:** task-level parent/child comparisons. The configured tolerance is a fraction of tasks that may regress, not the size of the average score decline.

The paired 95% bootstrap interval resamples task-level success differences 2,000 times. It is descriptive and is not the promotion criterion. It does not correct for adaptive candidate selection, multiple comparisons, or dependence among tasks. Eight fixture tasks per split do not support strong statistical conclusions.

## Ablations

`ablate` restores each selected genome component to its seed version and re-evaluates on validation tasks under the original budgets. This measures component dependence and interactions in the final program. It does not estimate an independent causal effect of an entire search method. An unchanged component is still evaluated to provide a control. Full run-level baselines, such as random search, no archive, and no recombination, require separate experiments and are not automated by this release.

## Publishable evidence

Keep the original configuration, task suite hash, seed, candidate snapshots, proposal rationales, diffs, aggregate metrics, resource usage, event head, and evaluation code revision. Include unsuccessful branches. Reports are snapshots that need to be regenerated after later actions.

The standalone report omits raw execution traces and private test cases, but includes diffs, mutation rationales, task IDs, and task-level scores. Custom project data may still appear in those fields. Review the report before making an experiment public.

## Current limits

This release supports Python function repair with JSON-serializable values, not an arbitrary repository shell. It has no automatic curriculum, persistent external memory service, weight optimization, executable skill dependency graph, or benchmark downloader. Budget estimates can differ from provider billing, and the ledger is not a payment limit. A live experiment can produce no useful mutations; all such outcomes remain recorded.
