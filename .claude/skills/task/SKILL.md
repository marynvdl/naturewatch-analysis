---
name: task
description: Start one pipeline task from docs/tasks. Use when Maryn says /task <file>, "do task NN", or "start the next task".
argument-hint: <docs/tasks/task-file.md>
disable-model-invocation: true
---

# Start a task

Task file: $ARGUMENTS

1. Read the task file and its parcel in `../naturewatch-app/docs/parcels/`. If no file was given, list the files in `docs/tasks/` and ask which one is meant.
2. Check the task is marked for naturewatch-analysis. If it is for naturewatch-app, stop and say to run it from that checkout.
3. Check the tasks above it in the parcel are ticked, and any "Start only after" condition is met. If not, stop and say what is missing.
4. Check the working tree is clean. Then `git switch main`, `git pull --ff-only`, and create a branch named for the task.
5. Read `docs/pipelines.md`, the recipe, and the step README the task names. Write a plan and wait for approval.
6. If the task has a billable step, write the expected cost and stop for approval before it. After an approved submission, record the job id, output path, status check, and cancel method in the step README, report them, and return.
7. Test on a small cropped sample. Run `pytest` for the code touched.
8. Record bytes stored, wall time, and peak memory in the step README.
9. Commit only the task's files, with no AI attribution. Push and open a pull request into `main`. Report the URL and checks. Do not merge.
10. Stop. Do not start the next task.
