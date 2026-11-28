import threading
import unittest
import urllib.request
from desk.server import Handler,LocalServer

class VideoPreviewPolicyTests(unittest.TestCase):
    def test_only_nba_html_can_embed_the_explicit_game_page_origin(self):
        server=LocalServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            base=f'http://127.0.0.1:{server.server_address[1]}'
            for path in ('/nba','/nba/','/','/btc-system','/nba.js'):
                with self.subTest(path=path),urllib.request.urlopen(base+path,timeout=3) as response:
                    policy=response.headers['Content-Security-Policy']
                    self.assertIn("frame-ancestors 'none'",policy)
                    if path in ('/nba','/nba/'):
                        self.assertIn('frame-src https://v2.streameast.ga/nba/',policy)
                        self.assertNotIn('frame-src *',policy)
                    else:self.assertNotIn('streameast',policy)
        finally:server.shutdown();server.server_close();worker.join()
