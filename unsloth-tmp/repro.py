def build(assessment, task, alias, profile=None, length=None, difficulty=None, note=None):
    return (assessment, task, alias)
_PARTIAL_SUBTASKS = [("a",""),("b","")]
assessment = object()
task_description = "x"
def f():
    sub_difficulty = 1
    return [
        build(
            assessment,
            f"{task_description} - {focus}" if task_description else focus,
            "alias", profile=profile,
            length="L", difficulty=sub_difficulty,
            note=f"Bounded subtask ({focus}) of a larger task."),
        for focus, _ in _PARTIAL_SUBTASKS
    ]
print(f())
