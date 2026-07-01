import unittest
from taskman.store import TaskStore


class TestTaskStore(unittest.TestCase):

    def test_add_and_list(self):
        store = TaskStore()
        task_id = store.add('Test Task 1', tags=['work', 'urgent'])
        task_id2 = store.add('Test Task 2', tags=['personal'])
        tasks = store.list()
        assert len(tasks) == 2
        assert tasks[0]['title'] == 'Test Task 1'
        assert tasks[1]['title'] == 'Test Task 2'
        assert set(tasks[0]['tags']) == {'work', 'urgent'}
        assert set(tasks[1]['tags']) == {'personal'}

    def test_list_by_priority(self):
        store = TaskStore()
        task_id1 = store.add('High Priority Task', priority=10)
        task_id2 = store.add('Low Priority Task', priority=1)
        tasks = store.list_by_priority()
        assert len(tasks) == 2
        assert tasks[0]['title'] == 'High Priority Task'
        assert tasks[1]['title'] == 'Low Priority Task'
        assert tasks[0]['priority'] == 10
        assert tasks[1]['priority'] == 1

    def test_filter_by_tag(self):
        store = TaskStore()
        task_id1 = store.add('Work Task', tags=['work', 'urgent'])
        task_id2 = store.add('Personal Task', tags=['personal'])
        tasks = store.filter_by_tag('work')
        assert len(tasks) == 1
        assert tasks[0]['title'] == 'Work Task'
        assert set(tasks[0]['tags']) == {'work', 'urgent'}

if __name__ == '__main__':
    unittest.main()