// This file implements an HTTP server that exposes a Bryant Evolution
// SAM Remote Access Module (e.g., SYSTXBBRCT01) over HTTP.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// evoRequest represents a request to the Evolution SAM. E.g., S1Z1FAN?
type evoRequest string

func (e evoRequest) isWrite() bool { return strings.Contains(string(e), "!") }

// evoRequest represents a response from the Evolution SAM. E.g., S1Z1FAN:AUTO
type evoResponse struct {
	// The echoed request, e.g., "S1Z1FAN"
	echoedCommand string

	// The payload of the response, e.g., "AUTO"
	payload string
}

func (e *evoResponse) String() string {
	return fmt.Sprintf("%s:%s", e.echoedCommand, e.payload)
}

// parseEvoResponse returns a parsed representation of `rawResp` as the
// response to `req`.
func parseEvoResponse(rawResp string, req evoRequest) (*evoResponse, error) {
	rawResp = sanitizeUtf8(rawResp) // Drop weird extended-ASCII degree symbol
	re := regexp.MustCompile("([[:alnum:]]+):(.*)")
	matchParts := re.FindStringSubmatch(rawResp)
	if matchParts == nil {
		return nil, fmt.Errorf("Unparseable response to %v: '%v'", req, rawResp)
	}
	echoedReq := matchParts[1]
	payload := matchParts[2]
	if !strings.HasPrefix(string(req), echoedReq) {
		return nil, fmt.Errorf("Protocol error: command %s vs response %s with echoed request %s", req, rawResp, echoedReq)
	}
	if strings.Contains(payload, "NAK") {
		return nil, fmt.Errorf("Rejected command: sent '%s', got '%s'", req, rawResp)
	}
	return &evoResponse{echoedReq, payload}, nil
}

// sanitizeUtf8 returns a copy of `s` that excludes utf.RuneError characters.
func sanitizeUtf8(s string) string {
	fixUtf := func(r rune) rune {
		if r == utf8.RuneError {
			return -1
		}
		return r
	}
	return strings.Map(fixUtf, s)
}

// deviceIo narrows the bufio.ReaderWriter interface to just those methods used
// in this implementation. It exists to allow cleanly substituing a fake
// implementation in unit tests.
type deviceIo interface {
	ReadString(delim byte) (string, error)
	WriteString(s string) (int, error)
	Flush() error
}

// pendingOp represents an operation to execute against the device.
type pendingOp struct {
	// Command to execute, e.g., S1Z1FAN?
	command evoRequest

	// Channel on which the result will be delivered, when available.
	ch chan opResult
}

// Result of executing a command against the device. Sum type.
type opResult struct {
	// Response, on success (else nil).
	response *evoResponse

	// Error, on failure (else nil)
	err error
}

// commandHandler implements an HTTP handler that exposes the device ASCII
// protocol.
type commandHandler struct {
	// Pending operations and concurrency control for them.
	mu            sync.Mutex
	workCond      *sync.Cond
	pendingReads  []pendingOp
	pendingWrites []pendingOp
}

// Starts the handler.
func (h *commandHandler) Open(device deviceIo) {
	h.workCond = sync.NewCond(&h.mu)

	// Goroutine to handle raw reads.
	respCh := make(chan string)
	go func() {
		for {
			resp, err := device.ReadString('\n')
			if err != nil {
				log.Fatalf("ReadString: %v", err)
			}
			resp = strings.TrimSpace(resp)
			if resp != "" {
				log.Printf("ReadString: %v", resp)
				respCh <- resp
			}
		}
	}()

	// Goroutine to handle command processing (writes and waiting for responses).
	go func() {
		for {
			func() {
				// Process one command
				op := h.blockForNextOp()

				const numCommandRetries = 3
				var lastError error = nil
				for i := 0; i < numCommandRetries; i++ {
					res, err := execCommand(device, respCh, op.command)
					if err != nil {
						log.Printf("(error on attempt count %v): %v", i, err)
						lastError = err
						continue
					}
					op.ch <- opResult{res, nil}
					close(op.ch)
					return
				}
				log.Printf("Permanent failure sending command %v", op.command)
				op.ch <- opResult{nil, lastError}
				close(op.ch)
			}()
		}
	}()
}

// Executes `cmd` against the device.
func execCommand(deviceWriter deviceIo, deviceReader <-chan string, cmd evoRequest) (*evoResponse, error) {
	// Device spec promises a response within 5 seconds.
	var commandTimeout = 6 * time.Second

	// Send the command
	deviceWriter.WriteString(fmt.Sprintf("%s\n", cmd))
	deviceWriter.Flush()

	select {
	case rawResp := <-deviceReader:
		return parseEvoResponse(rawResp, cmd)
	case <-time.After(commandTimeout):
		return nil, fmt.Errorf("Timeout: %v", cmd)
	}
}

// blockForNextOp returns the next operation to execute, blocking until one
// is available.
func (h *commandHandler) blockForNextOp() pendingOp {
	h.mu.Lock()
	defer h.mu.Unlock()

	// Wait for work to show up.
	for len(h.pendingReads) == 0 && len(h.pendingWrites) == 0 {
		h.workCond.Wait()
	}

	// Pick a write or a read to do, preferring writes. We prefer writes because
	// they correspond to something a human is trying to accomplish (say,
	// changing the temperature), and executing operations in a strict FIFO
	// order can result in annoyingly long delays, since the device takes
	// ~1.5s to execute one operation.
	if len(h.pendingWrites) > 0 {
		// Handle write
		op := h.pendingWrites[0]
		h.pendingWrites = h.pendingWrites[1:]
		return op
	} else if len(h.pendingReads) > 0 {
		// Handle read.
		op := h.pendingReads[0]
		h.pendingReads = h.pendingReads[1:]
		return op
	} else {
		panic("No work on wakeup")
	}
}

// addOp adds a new pending operation for cmd to the set of pending operations
// and returns the channel on which the result will eventually be written.
func (h *commandHandler) addOp(cmd evoRequest) <-chan opResult {
	h.mu.Lock()
	defer h.mu.Unlock()

	op := pendingOp{command: cmd, ch: make(chan opResult)}
	if cmd.isWrite() {
		h.pendingWrites = append(h.pendingWrites, op)
	} else {
		h.pendingReads = append(h.pendingReads, op)
	}
	h.workCond.Broadcast()
	return op.ch
}

func (h *commandHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Connection", "close")
	w.Header().Set("Content-Type", "application/json")
	defer r.Body.Close()
	cmdBytes, err := ioutil.ReadAll(r.Body)
	if err != nil {
		log.Printf("Failed reading body: %v", err)
		http.Error(w, fmt.Sprintf("%v", err), http.StatusInternalServerError)
		return
	}
	cmd := evoRequest(string(cmdBytes))

	resultCh := h.addOp(cmd)
	result := <-resultCh
	if result.err != nil {
		// Error
		log.Printf("Failed sending command %v: %v", cmd, result.err)
		http.Error(w, fmt.Sprintf("Failed to send %v: %v", cmd, result.err),
			http.StatusInternalServerError)
		return
	}
	log.Printf("Command %v, Response: %v", cmd, result.response)
	fmt.Fprintf(w, `{"response": "%s"}`+"\n", result.response.payload)
}

func exportNewHandler(device deviceIo) *http.Server {
	handler := new(commandHandler)
	handler.Open(device)
	m := http.NewServeMux()
	m.Handle("/command", handler)

	srv := &http.Server{
		Addr:         ":8080",
		ReadTimeout:  20 * time.Second,
		WriteTimeout: 85 * time.Second,
		Handler:      m,
	}
	srv.SetKeepAlivesEnabled(false)
	return srv
}

func main() {
	device := flag.String("device", "/dev/ttyUSB0", "Name of file corresponding to device to control")
	flag.Parse()

	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if err := exec.Command("stty", "-F", *device, "9600", "cs8", "-cstopb", "-parenb", "-echo").Run(); err != nil {
		log.Fatalf("stty: %s", err)
	}
	ttyFile := "/dev/ttyUSB0"
	serialFile, err := os.OpenFile(ttyFile, os.O_RDWR, 0)
	if err != nil {
		log.Fatalf("OpenFile: %v", err)
	}
	log.Printf("Opened device file: %v", ttyFile)
	srv := exportNewHandler(bufio.NewReadWriter(bufio.NewReader(serialFile), bufio.NewWriter(serialFile)))
	log.Fatal(srv.ListenAndServe())
}
