# ── State ──
%store myvar              # save to disk
%store -r myvar           # restore from disk
%store -z                 # wipe all stored
%who                      # list all variables
%whos                     # list with types and values
%who_ls                   # list as a Python list (usable in expressions!)

var_names = %who_ls       # ['js_files', 'result', 'u', 'url']
print(len(var_names))     # 4

# ── Timing ──
%time sum(range(10**6))           # one-shot timing
%timeit -n 100 -r 5 sum(range(1000))  # statistical timing
%prun my_function()               # line profiler
%memit big_array                  # memory profiler (needs %memit extension)

# ── File system ──
%pwd                      # current dir
%cd /tmp                  # change dir (persists across cells!)
%ls                       # ls
%mkdir test_dir
%cp file.txt file.bak

# ── Debugging ──
%debug                    # drop into pdb at the last exception
%pdb on                   # auto-drop into pdb on any exception
%load_ext memory_profiler # load an extension
%hist                     # command history

# ── Shell passthrough ──
!nmap -sV 10.10.10.5      # run any shell command
!cat /etc/hosts | head

# ── Cell magics ──
%%bash                    # entire cell is bash
echo "I'm in bash"
whoami

%%html                    # entire cell rendered as HTML
<table><tr><td>hi</td></tr></table>

%%javascript              # entire cell runs as JS in the frontend
alert("from the kernel, with love")

%%python2                 # run cell in Python 2 (if installed)
print "hello"

%%writefile scan.py       # write cell content to a file
import requests
r = requests.get("https://target")