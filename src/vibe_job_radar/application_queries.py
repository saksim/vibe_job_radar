"""Read application configuration without opening an operational data store."""
import os


class ApplicationQueries:
    def __init__(self, application):
        self.application = application

    def bootstrap(self, data):
        config = self.application.workspace.config
        return {
            'roles': {key: value.get('label', key) for key, value in config['roles'].items()},
            'platforms': {key: {
                'label': value.get('label', key), 'domains': list(value['domains']),
                'manual_import': 'available', 'automatic_login': 'not_implemented',
                'live_acceptance': 'not_verified',
            } for key, value in config['platforms'].items()},
            'brave_key_configured': bool(os.environ.get('BRAVE_SEARCH_API_KEY', '')),
            'acquisition_sites': self.application.guided.registry.describe(),
        }
