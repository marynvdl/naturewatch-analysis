---
name: task
description: Start one pipeline task. Its file is in ../naturewatch-app/docs/tasks. Use when the user says /task <file>, "do task NN", or "start the next task".
argument-hint: <../naturewatch-app/docs/tasks/task-file.md>
disable-model-invocation: true
---

# Start a task

Task file: $ARGUMENTS

1. Read the task file and its parcel in `../naturewatch-app/docs/parcels/`. If no file was given, list the analysis tasks in `../naturewatch-app/docs/tasks/` and ask which one is meant.
2. Check the task is marked for naturewatch-analysis. If it is for naturewatch-app, do it in `../naturewatch-app`, following its CLAUDE.md and its task skill.
3. Check the tasks above it in the parcel are ticked, and any "Start only after" condition is met. If not, stop and say what is missing.
4. Check the working tree is clean. Then `git switch main`, `git pull --ff-only`, and create a branch `<type>/task-NN-<name>` (CLAUDE.md, Git).
5. Read `docs/pipelines.md`, the recipe, and the step README the task names. Write a plan and wait for approval.
6. If the task has a billable step, write the expected cost and stop for approval before it. After an approved submission, record the job id, output path, status check, and cancel method in the step README, report them, and return.
7. Test on a small cropped sample. Run `pytest` for the code touched.
8. Record bytes stored, wall time, and peak memory in the step README.
9. Commit only the task's files, with no AI attribution, as in CLAUDE.md. Push and open a pull request into `main`, with its title and description as in CLAUDE.md. Report the URL and checks. Do not merge.
10. Stop. Do not start the next task.
