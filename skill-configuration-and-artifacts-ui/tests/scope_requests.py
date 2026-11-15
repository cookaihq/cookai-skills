def file_edit(target, changes):
    """Explicit file edits, as opposed to a user requesting an effective-source switch."""
    return {'files': {target: changes}, 'switches': {},
            'file_edits': {target: list(changes) if isinstance(changes, dict) else []}}
