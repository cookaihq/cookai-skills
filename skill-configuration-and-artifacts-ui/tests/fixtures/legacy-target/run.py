"""Synthetic existing business target: keep this entry and its output behavior."""
import json
from pathlib import Path


def run(project):
    project = Path(project)
    path = project / 'settings.json'
    settings = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    result = {'message': settings.get('greeting', 'Hello'), 'project': str(project.resolve())}
    (project / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    return result


if __name__ == '__main__':
    import sys
    print(json.dumps(run(sys.argv[1])))
