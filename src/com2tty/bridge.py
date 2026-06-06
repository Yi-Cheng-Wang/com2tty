import sys
import os
import select
import argparse
import signal
import traceback

def cleanup_symlink(path):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
            sys.stderr.write(f"Removed symlink: {path}\n")
            sys.stderr.flush()
        except Exception as e: # pragma: no cover
            sys.stderr.write(f"Failed to remove symlink {path}: {e}\n")
            sys.stderr.flush()

def main():
    parser = argparse.ArgumentParser(description="com2tty WSL Bridge Helper")
    parser.add_argument(
        "-s", "--symlink",
        required=True,
        help="Target symlink path for the pseudo-terminal device."
    )
    args = parser.parse_args()
    
    target_path = args.symlink
    created_symlink = None
    
    # We must keep both master and slave descriptors open.
    # Keeping slave_fd open prevents EIO errors on the master side when
    # WSL clients open and close the virtual serial port.
    master_fd = None
    slave_fd = None
    
    try:
        master_fd, slave_fd = os.openpty()
        slave_name = os.ttyname(slave_fd)
        
        sys.stderr.write(f"Created pseudo-terminal: master_fd={master_fd}, slave={slave_name}\n")
        sys.stderr.flush()
        
        # Attempt to create the symlink at target path
        try:
            if os.path.lexists(target_path):
                os.unlink(target_path)
            os.symlink(slave_name, target_path)
            created_symlink = target_path
            sys.stderr.write(f"Successfully symlinked {target_path} -> {slave_name}\n")
            sys.stderr.flush()
        except PermissionError:
            # Fallback to /tmp if write permission to dev is denied
            basename = os.path.basename(target_path)
            fallback_path = f"/tmp/{basename}"
            sys.stderr.write(f"Warning: Permission denied creating symlink at {target_path}.\n")
            sys.stderr.write(f"Attempting fallback to user-writable path: {fallback_path}...\n")
            sys.stderr.flush()
            
            if os.path.lexists(fallback_path):
                os.unlink(fallback_path)
            os.symlink(slave_name, fallback_path)
            created_symlink = fallback_path
            
            sys.stderr.write(f"Fallback successful: {fallback_path} -> {slave_name}\n")
            sys.stderr.write("--------------------------------------------------\n")
            sys.stderr.write(f"To use the desired device path '{target_path}', please run this command ONCE in WSL:\n")
            sys.stderr.write(f"  sudo ln -sf {fallback_path} {target_path}\n")
            sys.stderr.write("--------------------------------------------------\n")
            sys.stderr.flush()
            
        # Select loop
        # 0 is stdin, master_fd is the pseudo-terminal master
        sys.stderr.write("WSL bridge enter main loop.\n")
        sys.stderr.flush()
        
        while True:
            # select blocks until data is available on stdin or master_fd
            r, w, x = select.select([0, master_fd], [], [])
            
            if 0 in r:
                data = os.read(0, 4096)
                if not data:
                    sys.stderr.write("EOF on stdin. Exiting.\n")
                    sys.stderr.flush()
                    break
                os.write(master_fd, data)
                
            if master_fd in r:
                # Read from the virtual serial port
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        # Should not happen typically while slave_fd is kept open,
                        # but handle it gracefully if it does.
                        sys.stderr.write("EOF on PTY master. Exiting.\n")
                        sys.stderr.flush()
                        break
                    os.write(1, data)
                except OSError as e:
                    # In case of EIO (Input/output error) when slave closes,
                    # just ignore it and continue since we keep slave_fd open.
                    if e.errno == 5: # EIO
                        continue
                    else:
                        raise e
                        
    except KeyboardInterrupt: # pragma: no cover
        sys.stderr.write("WSL bridge interrupted by signal.\n")
        sys.stderr.flush()
    except Exception as e: # pragma: no cover
        sys.stderr.write(f"WSL bridge error: {traceback.format_exc()}\n")
        sys.stderr.flush()
    finally:
        # Clean up symlink and file descriptors
        if created_symlink:
            cleanup_symlink(created_symlink)
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
        sys.stderr.write("WSL bridge shut down.\n")
        sys.stderr.flush()

if __name__ == "__main__": # pragma: no cover
    main()
