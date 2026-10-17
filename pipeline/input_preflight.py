"""Small read-only input check collector shared by existing production entries."""


class InputChecks:
    def __init__(self):
        self.errors = []

    def run(self, name, check):
        try:
            return check()
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, RuntimeError) as error:
            self.errors.append({'check': name, 'message': str(error)})
            return None

    def expect(self, name, condition, message):
        if not condition:
            self.errors.append({'check': name, 'message': message})

    def report(self):
        return {'status': 'failed' if self.errors else 'passed', 'read_only': True,
                'errors': self.errors,
                'limitations': '仅输入预检；不代表技术成片、听审、连续观看或平台发布验收'}

    def require(self):
        if self.errors:
            raise ValueError('输入预检失败：\n' + '\n'.join(
                f"- {item['check']}: {item['message']}" for item in self.errors))


def object_input(value):
    if not isinstance(value, dict):
        raise ValueError('输入记录必须为JSON对象')
    return value
