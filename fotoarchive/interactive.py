"""Skip superseded UI work before it can hold up the processing queue."""
SEARCH_ACTIONS = {'browse', 'search', 'similar', 'face_search'}


def obsolete_command(command, state):
    if state is None:
        return False
    action = command['action']
    if action in SEARCH_ACTIONS | {'search_page', 'verify_more', 'clear_search', 'search_places', 'match_moments'}:
        return command.get('id') != state[0]
    if action == 'faces' and 'selection_serial' in command:
        return command['selection_serial'] != state[1] or command.get('request_id') != state[0]
    return False
