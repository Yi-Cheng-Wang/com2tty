import unittest
from unittest.mock import MagicMock, patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from com2tty.bridge import main, cleanup_symlink

class TestBridgeScript(unittest.TestCase):
    
    @patch("os.path.exists")
    @patch("os.unlink", create=True)
    @patch("sys.stderr.write")
    def test_cleanup_symlink(self, mock_write, mock_unlink, mock_exists):
        mock_exists.return_value = True
        cleanup_symlink("/tmp/tty")
        mock_unlink.assert_called_once_with("/tmp/tty")
        
        # Test exception path
        mock_unlink.side_effect = Exception("unlink failed")
        cleanup_symlink("/tmp/tty")
        mock_write.assert_called()

    @patch("sys.argv", ["bridge.py", "--symlink", "/dev/ttyUSB0"])
    @patch("os.openpty", create=True)
    @patch("os.ttyname", create=True)
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.write")
    @patch("os.unlink", create=True)
    @patch("os.close")
    def test_bridge_main_normal(self, mock_close, mock_unlink, mock_write, mock_read, mock_select, mock_symlink, mock_lexists, mock_ttyname, mock_openpty):
        mock_openpty.return_value = (3, 4)
        mock_ttyname.return_value = "/dev/pts/1"
        mock_lexists.return_value = False
        
        def fake_select(*args, **kwargs):
            if getattr(fake_select, "calls", 0) == 0:
                fake_select.calls = 1
                return ([0], [], [])
            elif fake_select.calls == 1:
                fake_select.calls = 2
                return ([3], [], [])
            else:
                raise KeyboardInterrupt()
        
        mock_select.side_effect = fake_select
        
        mock_read.side_effect = [b"stdin_data", b"pty_data"]
        
        main()
        
        mock_symlink.assert_called_with("/dev/pts/1", "/dev/ttyUSB0")
        mock_write.assert_any_call(3, b"stdin_data")
        mock_write.assert_any_call(1, b"pty_data")
        mock_close.assert_any_call(4)
        mock_close.assert_any_call(3)

    @patch("sys.argv", ["bridge.py", "--symlink", "/dev/ttyUSB0"])
    @patch("os.openpty", create=True)
    @patch("os.ttyname", create=True)
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_bridge_main_permission_fallback(self, mock_unlink, mock_close, mock_read, mock_select, mock_symlink, mock_lexists, mock_ttyname, mock_openpty):
        mock_openpty.return_value = (3, 4)
        mock_ttyname.return_value = "/dev/pts/1"
        
        # First call is for target_path (return False), second call is for fallback_path (return True) to trigger unlink
        mock_lexists.side_effect = [False, True]
        
        def fake_symlink(src, dst):
            if dst == "/dev/ttyUSB0":
                raise PermissionError("Access denied")
            return None
            
        mock_symlink.side_effect = fake_symlink
        
        mock_select.return_value = ([0], [], [])
        mock_read.return_value = b""
        
        main()
        
        mock_unlink.assert_any_call("/tmp/ttyUSB0")
        mock_symlink.assert_any_call("/dev/pts/1", "/tmp/ttyUSB0")

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True)
    @patch("os.ttyname", create=True)
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_bridge_main_eio_and_eof(self, mock_unlink, mock_close, mock_read, mock_select, mock_symlink, mock_lexists, mock_ttyname, mock_openpty):
        mock_openpty.return_value = (3, 4)
        mock_ttyname.return_value = "/dev/pts/1"
        
        # Trigger EOF on master_fd loop
        def fake_select(*args, **kwargs):
            return ([3], [], []) 
                
        mock_select.side_effect = fake_select
        
        eio_err = OSError()
        eio_err.errno = 5
        
        # Read returns EIO first, then EOF
        mock_read.side_effect = [eio_err, b""] 
        
        # Make close throw exception to hit lines 120-121, 125-126
        mock_close.side_effect = Exception("Mocked close error")
        
        main()

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True)
    @patch("os.ttyname", create=True)
    @patch("os.path.lexists")
    @patch("os.symlink", create=True)
    @patch("select.select")
    @patch("os.read")
    @patch("os.close")
    @patch("os.unlink", create=True)
    def test_bridge_main_generic_oserror(self, mock_unlink, mock_close, mock_read, mock_select, mock_symlink, mock_lexists, mock_ttyname, mock_openpty):
        mock_openpty.return_value = (3, 4)
        mock_ttyname.return_value = "/dev/pts/1"
        
        def fake_select(*args, **kwargs):
            return ([3], [], []) 
                
        mock_select.side_effect = fake_select
        
        gen_err = OSError("generic")
        gen_err.errno = 99
        
        # Read returns generic OSError to trigger 'raise e'
        mock_read.side_effect = [gen_err] 
        
        main()

    @patch("sys.argv", ["bridge.py", "--symlink", "/tmp/tty"])
    @patch("os.openpty", create=True)
    def test_bridge_main_fatal_exception(self, mock_openpty):
        mock_openpty.side_effect = Exception("Fatal OS Error")
        main()

if __name__ == "__main__":
    unittest.main()
