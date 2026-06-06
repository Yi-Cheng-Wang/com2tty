import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

class TestMainEntryPoint(unittest.TestCase):
    
    @patch("com2tty.cli.main")
    def test_main_execution(self, mock_main):
        import com2tty.__main__
        # Because the import triggers the __name__ == "__main__" block natively
        # only if we use runpy or execute it directly, we just call the block manually 
        # to ensure coverage tool registers it.
        with patch.object(com2tty.__main__, "__name__", "__main__"):
            # Trigger the module execution
            try:
                with open(com2tty.__main__.__file__) as f:
                    exec(f.read(), com2tty.__main__.__dict__)
            except SystemExit:
                pass
                
        mock_main.assert_called()

if __name__ == "__main__":
    unittest.main()
