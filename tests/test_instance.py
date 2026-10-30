import threading
import unittest
from pathlib import Path

from workhistory import instance
from workhistory.server import App, create_server, create_server_in

from tests.helpers import TempDB


class InstanceTests(unittest.TestCase):
    def test_one_app_per_database(self):
        with TempDB() as db:
            path = Path(db.path)
            self.assertTrue(instance.claim(path))
            self.assertFalse(instance.claim(path))  # a second launch for the same database
            self.assertTrue(instance.claim(path.with_name("demo.db")))  # another database may run

    def test_finds_the_app_for_its_database_on_any_port(self):
        with TempDB() as db, TempDB() as other_db:
            mine, other = App(db, None), App(other_db, None)
            servers = [create_server(other, 0), create_server(mine, 0)]
            try:
                for s in servers:
                    threading.Thread(target=s.serve_forever, daemon=True).start()
                ports = [s.server_address[1] for s in servers]
                # The first port answers too, but with another database: skipped.
                self.assertEqual(instance.find(ports, Path(db.path), wait_s=0), ports[1])
                self.assertIsNone(instance.find(ports[:1], Path(db.path), wait_s=0))
            finally:
                for s in servers:
                    s.shutdown()
                    s.server_close()

    def test_moves_to_the_next_free_port(self):
        with TempDB() as db:
            taken = create_server(App(db, None), 0)  # another program on the first port
            try:
                port = taken.server_address[1]
                app = App(db, None)
                server = create_server_in(app, [port, 0])  # 0 = any free port
                try:
                    self.assertNotEqual(server.server_address[1], port)
                    self.assertEqual(app.port, server.server_address[1])
                finally:
                    server.server_close()
                with self.assertRaises(OSError):
                    create_server_in(App(db, None), [port])
            finally:
                taken.server_close()


if __name__ == "__main__":
    unittest.main()
