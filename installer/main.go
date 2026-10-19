// LazyTrack installer for Windows and Mac.
//
// One file your friends double-click. It:
//  1. copies LazyTrack (embedded in this program) into the user's folder, no admin needed
//  2. downloads a private, portable Python (python-build-standalone) next to it
//  3. installs what LazyTrack needs (pip, ~1 GB: mostly the camera AI)
//  4. adds shortcuts (Start menu + desktop + start with the computer) and an uninstaller
//  5. starts LazyTrack and opens its app window
//
// Progress is shown in a small window of its own (WebKit on Mac, Edge's app mode on Windows), in
// LazyTrack's style. Only if that window can't open does it fall back to the browser.
// The code stays as normal files, so LazyTrack's own updater keeps it up to date afterwards:
// this installer is only ever needed once.
//
// Build: installer/build.sh (makes dist/LazyTrack-Setup.exe and dist/Install-LazyTrack-Mac.zip).
package main

import (
	"archive/tar"
	"archive/zip"
	"bufio"
	"bytes"
	"compress/gzip"
	_ "embed"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

//go:embed payload.zip
var payload []byte

//go:embed progress.html
var progressPage []byte

const pythonRelease = "20241016"
const pythonVersion = "3.12.7"
const port = 8042

// ---------------------------------------------------------------- what the progress page shows
type Step struct {
	Name   string `json:"name"`
	State  string `json:"state"` // "wait", "run", "done", "fail"
	Detail string `json:"detail"`
	Pct    int    `json:"pct"` // -1 = no percentage
}

type State struct {
	Steps []*Step  `json:"steps"`
	Log   []string `json:"log"`
	Error string   `json:"error"`
	Done  bool     `json:"done"`
	URL   string   `json:"url"`
	Dir   string   `json:"dir"`
}

var (
	mu    sync.Mutex
	state = State{Steps: []*Step{
		{Name: "Copying Mochi"},
		{Name: "Getting Python"},
		{Name: "Installing what Mochi needs (about 1 GB, the longest part)"},
		{Name: "Getting Mochi ready"},
		{Name: "Adding shortcuts"},
		{Name: "Starting Mochi"},
	}}
	retry = make(chan bool, 1)
	hello = make(chan bool, 1) // the progress page loaded (so its window really is on screen)
)

func setStep(i int, st, detail string, pct int) {
	mu.Lock()
	defer mu.Unlock()
	state.Steps[i].State, state.Steps[i].Detail, state.Steps[i].Pct = st, detail, pct
}

func logLine(s string) {
	s = strings.TrimSpace(s)
	if s == "" {
		return
	}
	mu.Lock()
	state.Log = append(state.Log, s)
	if len(state.Log) > 200 {
		state.Log = state.Log[len(state.Log)-200:]
	}
	mu.Unlock()
}

// ---------------------------------------------------------------- start
func main() {
	if len(os.Args) > 1 && os.Args[1] == "--uninstall" {
		uninstall()
		return
	}
	dir := appDir()
	again := fileExists(filepath.Join(dir, ".venv"))
	msg := "Mochi will be installed for you on this computer (no admin needed).\n\n" +
		"It downloads about 1 GB (mostly the camera AI), so it takes a few minutes. A small window shows the progress.\n\nInstall now?"
	if again {
		msg = "Mochi is already installed here.\n\nRepair it? Your data and settings are kept."
	}
	if !ask("Install Mochi", msg) {
		return
	}
	state.Dir = dir

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		tell("Mochi", "Couldn't start the installer: "+err.Error())
		return
	}
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(progressPage)
	})
	http.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(state)
	})
	http.HandleFunc("/hello", func(w http.ResponseWriter, r *http.Request) {
		select {
		case hello <- true:
		default:
		}
		w.Write([]byte("ok"))
	})
	http.HandleFunc("/open", func(w http.ResponseWriter, r *http.Request) {
		openApp(dir)
		w.Write([]byte("ok"))
	})
	http.HandleFunc("/retry", func(w http.ResponseWriter, r *http.Request) {
		select {
		case retry <- true:
		default:
		}
		w.Write([]byte("ok"))
	})
	go http.Serve(ln, nil)
	page := fmt.Sprintf("http://%s/", ln.Addr().String())
	closeWindow := showProgress(page)
	go func() { // no sign of the window after a while: show the progress in the browser instead
		select {
		case <-hello:
		case <-time.After(12 * time.Second):
			closeWindow()
			closeWindow = func() {}
			openURL(page)
		}
	}()

	for {
		if err := install(dir); err != nil {
			mu.Lock()
			state.Error = err.Error()
			mu.Unlock()
			<-retry // "Try again" on the page
			mu.Lock()
			state.Error = ""
			mu.Unlock()
			continue
		}
		break
	}
	mu.Lock()
	state.Done, state.URL = true, fmt.Sprintf("http://localhost:%d", port)
	mu.Unlock()
	openApp(dir)                // LazyTrack's own window, not the browser
	time.Sleep(8 * time.Second) // let the page say it's done, then close the progress window and quit
	closeWindow()
	time.Sleep(1 * time.Second)
}

// ---------------------------------------------------------------- the steps
func install(dir string) error {
	steps := []func(string) error{copyApp, getPython, pipInstall, initApp, addShortcuts, startApp}
	for i, f := range steps {
		mu.Lock()
		skip := state.Steps[i].State == "done"
		mu.Unlock()
		if skip {
			continue
		}
		setStep(i, "run", "", -1)
		if err := f(dir); err != nil {
			setStep(i, "fail", err.Error(), -1)
			return fmt.Errorf("%s: %v", state.Steps[i].Name, err)
		}
		mu.Lock()
		state.Steps[i].State, state.Steps[i].Pct, state.Steps[i].Detail = "done", -1, ""
		mu.Unlock()
	}
	return nil
}

// 1. unzip LazyTrack's files (an existing install keeps its Python and packages)
func copyApp(dir string) error {
	if fileExists(filepath.Join(dir, ".venv")) { // a repair: stop the running copy so its files can be replaced
		setStep(0, "run", "stopping the Mochi that's running", -1)
		stopRunning(dir)
	}
	zr, err := zip.NewReader(bytes.NewReader(payload), int64(len(payload)))
	if err != nil {
		return err
	}
	for n, f := range zr.File {
		dest := filepath.Join(dir, filepath.FromSlash(f.Name))
		if !strings.HasPrefix(dest, filepath.Clean(dir)+string(os.PathSeparator)) {
			continue
		}
		if f.FileInfo().IsDir() {
			os.MkdirAll(dest, 0o755)
			continue
		}
		os.MkdirAll(filepath.Dir(dest), 0o755)
		rc, err := f.Open()
		if err != nil {
			return err
		}
		mode := os.FileMode(0o644)
		if f.Mode()&0o111 != 0 {
			mode = 0o755
		}
		out, err := os.OpenFile(dest, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, mode)
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(out, rc)
		rc.Close()
		out.Close()
		if err != nil {
			return err
		}
		setStep(0, "run", f.Name, (n+1)*100/len(zr.File))
	}
	// the installer stays next to the app, so it can uninstall later
	if self, err := os.Executable(); err == nil {
		copyFile(self, filepath.Join(dir, uninstallerName()))
	}
	return nil
}

// 2. a private Python, so nobody has to install Python themselves
func getPython(dir string) error {
	if fileExists(pythonExe(dir)) {
		setStep(1, "run", "already there", 100)
		return nil
	}
	url := fmt.Sprintf("https://github.com/astral-sh/python-build-standalone/releases/download/%s/cpython-%s+%s-%s-install_only.tar.gz",
		pythonRelease, pythonVersion, pythonRelease, pythonTarget())
	logLine("Downloading " + url)
	// no total limit (slow internet is fine), but give up on a dead connection instead of waiting forever
	client := &http.Client{Transport: &http.Transport{Proxy: http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 30 * time.Second}).DialContext,
		TLSHandshakeTimeout:   30 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second}}
	resp, err := client.Get(url)
	if err != nil {
		return fmt.Errorf("couldn't download Python (is the internet on?): %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return fmt.Errorf("couldn't download Python: %s", resp.Status)
	}
	pr := &progressReader{r: resp.Body, total: resp.ContentLength, step: 1, label: "downloading"}
	gz, err := gzip.NewReader(pr)
	if err != nil {
		return err
	}
	tmp := filepath.Join(dir, "python.partial")
	os.RemoveAll(tmp)
	tr := tar.NewReader(gz)
	for {
		h, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		dest := filepath.Join(tmp, filepath.FromSlash(h.Name))
		switch h.Typeflag {
		case tar.TypeDir:
			os.MkdirAll(dest, 0o755)
		case tar.TypeSymlink:
			os.MkdirAll(filepath.Dir(dest), 0o755)
			os.Symlink(h.Linkname, dest)
		case tar.TypeReg:
			os.MkdirAll(filepath.Dir(dest), 0o755)
			out, err := os.OpenFile(dest, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, os.FileMode(h.Mode)&0o777|0o600)
			if err != nil {
				return err
			}
			_, err = io.Copy(out, tr)
			out.Close()
			if err != nil {
				return err
			}
		}
	}
	// the archive has one folder, "python"
	os.RemoveAll(filepath.Join(dir, "python"))
	if err := os.Rename(filepath.Join(tmp, "python"), filepath.Join(dir, "python")); err != nil {
		return err
	}
	os.RemoveAll(tmp)
	return nil
}

// 3. LazyTrack's own Python folder (.venv) + its packages
func pipInstall(dir string) error {
	venvPy := venvPython(dir)
	if os.Getenv("LT_TEST_SKIP_PIP") != "" { // tests on a machine without pypi: just the venv
		return run(dir, 2, pythonExe(dir), "-m", "venv", "--system-site-packages", ".venv")
	}
	if !fileExists(venvPy) {
		setStep(2, "run", "making Mochi's Python folder", -1)
		if err := run(dir, 2, pythonExe(dir), "-m", "venv", ".venv"); err != nil {
			return err
		}
	}
	setStep(2, "run", "updating pip", -1)
	if err := run(dir, 2, venvPy, "-m", "pip", "install", "--upgrade", "pip"); err != nil {
		return err
	}
	if err := run(dir, 2, venvPy, "-m", "pip", "install", "-r", requirementsFile()); err != nil {
		return err
	}
	// the camera AI pulls in a second OpenCV that breaks the first: keep exactly one
	run(dir, 2, venvPy, "-m", "pip", "uninstall", "-y", "opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless")
	return run(dir, 2, venvPy, "-m", "pip", "install", "opencv-python")
}

// 4. first-time setup of the data folder (~/.lazytrack)
func initApp(dir string) error {
	return run(dir, 3, venvPython(dir), "-m", "lazytrack", "init")
}

// 5. shortcuts and uninstaller: see platform_*.go
func addShortcuts(dir string) error { return makeShortcuts(dir) }

// 6. start it and wait until it answers
func startApp(dir string) error {
	if err := launch(dir); err != nil {
		return err
	}
	for i := 0; i < 90; i++ {
		c := http.Client{Timeout: 2 * time.Second}
		if r, err := c.Get(fmt.Sprintf("http://127.0.0.1:%d/", port)); err == nil {
			r.Body.Close()
			return nil
		}
		setStep(5, "run", "waiting for Mochi to start", -1)
		time.Sleep(time.Second)
	}
	return fmt.Errorf("Mochi didn't start. Try the Mochi shortcut; if it still doesn't open, send a screenshot of this page")
}

// ---------------------------------------------------------------- helpers
// run a command, showing its output on the progress page; pip's lines give a rough idea of where it is
func run(dir string, step int, name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "PYTHONUTF8=1", "PIP_DISABLE_PIP_VERSION_CHECK=1", "PYTHONIOENCODING=utf-8")
	hideWindow(cmd)
	out, err := cmd.StdoutPipe()
	if err != nil {
		return err
	}
	cmd.Stderr = cmd.Stdout
	if err := cmd.Start(); err != nil {
		return err
	}
	sc := bufio.NewScanner(out)
	sc.Buffer(make([]byte, 64*1024), 1024*1024)
	seen := 0
	for sc.Scan() {
		line := sc.Text()
		logLine(line)
		t := strings.TrimSpace(line)
		if strings.HasPrefix(t, "Collecting ") || strings.HasPrefix(t, "Downloading ") || strings.HasPrefix(t, "Installing collected") {
			seen++
			what := strings.Fields(t)
			detail := t
			if len(what) > 1 {
				detail = strings.TrimSuffix(what[0], "ing") + "ing " + shortName(what[1])
			}
			// ~120 lines for a full install; never claim 100% before it's really done
			pct := seen * 95 / 120
			if pct > 95 {
				pct = 95
			}
			setStep(step, "run", detail, pct)
		}
	}
	if err := cmd.Wait(); err != nil {
		return fmt.Errorf("%s failed (%v). The details are in the log below", filepath.Base(name), err)
	}
	return nil
}

func shortName(s string) string {
	s = filepath.Base(strings.Split(s, "?")[0])
	if i := strings.IndexAny(s, "-="); i > 0 && strings.HasSuffix(s, ".whl") {
		return s[:i]
	}
	return s
}

type progressReader struct {
	r     io.Reader
	total int64
	n     int64
	step  int
	label string
	last  time.Time
}

func (p *progressReader) Read(b []byte) (int, error) {
	n, err := p.r.Read(b)
	p.n += int64(n)
	if time.Since(p.last) > 300*time.Millisecond && p.total > 0 {
		p.last = time.Now()
		setStep(p.step, "run", fmt.Sprintf("%s %d of %d MB", p.label, p.n>>20, p.total>>20), int(p.n*100/p.total))
	}
	return n, err
}

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

func pythonTarget() string {
	switch runtime.GOOS + "/" + runtime.GOARCH {
	case "windows/amd64":
		return "x86_64-pc-windows-msvc"
	case "darwin/arm64":
		return "aarch64-apple-darwin"
	case "darwin/amd64":
		return "x86_64-apple-darwin"
	case "linux/arm64":
		return "aarch64-unknown-linux-gnu"
	}
	return "x86_64-unknown-linux-gnu"
}
