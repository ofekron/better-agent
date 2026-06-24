You are reviewing captured project updates for the project-structure skill.

Goal: decide which updates should become edits to the project-structure skill,
then make the required edits directly.

Requirement guard: project structure must never contradict user requirements.
Before deciding, check the stored user requirements with the get-requirements
skill/search workflow. If the current docs or proposed edits conflict with user
requirements, do not guess; flag the contradiction to the user and wait for
direction.

Autonomy rule: minimize user intervention. Use the project files, captured
updates, existing project-structure sections, and get-requirements results to
decide what to edit. Ask the user only as a last resort when a real requirement
conflict or missing product decision cannot be resolved from available context.

## Project directory
$project_cwd

## Skill files location
$skill_dir

## Sections available (read each one before editing)
$sections_list

## Captured updates ($updates_count total)
$updates_text

Start by reading the relevant skill files and checking requirements, then apply
the edits that are clearly required. End with a concise summary of what changed
and any contradiction that still needs user direction.
