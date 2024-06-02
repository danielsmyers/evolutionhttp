package main

import (
	"io/ioutil"
	"log"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// Fake evolution device that simulates a serial port using channels.
type fakeDevice struct {
	// Channels for emulating a serial port to/from the device.
	toDeviceChan   chan string
	fromDeviceChan chan string

	// Channels for exposing data to the unit test and getting data to send.
	testCommandChan  chan string
	testResponseChan chan string
}

func newFakeDevice() *fakeDevice {
	return &fakeDevice{
		toDeviceChan:     make(chan string),
		fromDeviceChan:   make(chan string),
		testCommandChan:  make(chan string),
		testResponseChan: make(chan string),
	}
}

// Implements the Read call as seen by the server -- i.e., this reads data
// from the device.
func (d *fakeDevice) ReadString(delim byte) (string, error) {
	nextStr := <-d.fromDeviceChan
	if nextStr[len(nextStr)-1] != delim {
		log.Fatalf("Next string to read did not end with expected delimeter: %v, %v", nextStr, delim)
	}
	return nextStr, nil
}

// Implements the Write call as seen by the server -- i.e., this writes data
// to the device.
func (d *fakeDevice) WriteString(s string) (n int, err error) {
	d.toDeviceChan <- s
	return len(s), nil
}

func (d *fakeDevice) Flush() error {
	return nil
}

// Start the fake device's command handling goroutine. This emulates the
// device itself.
func (d *fakeDevice) start() {
	go func() {
		for {
			// Wait for the next command from the server.
			cmd := <-d.toDeviceChan
			cmd = strings.TrimSpace(cmd)

			// Send the command to the test driver
			d.testCommandChan <- cmd

			// Wait for the response from the test driver
			response := <-d.testResponseChan

			// Send the response back to the server.
			d.fromDeviceChan <- response + "\n"
			d.fromDeviceChan <- "\n"
		}
	}()
}

func TestWritePrioritization(t *testing.T) {
	// Test plan: send a read to the server and wait for it to send the request
	// to the device. With it hanging, issue a second read and then a write.
	// Unblock all operations and verify that the write completes before the
	// second read.

	fakeDevice := newFakeDevice()
	fakeDevice.start()

	handler := new(commandHandler)
	handler.Open(fakeDevice)

	server := httptest.NewServer(handler)
	defer server.Close()

	// Channels to collect HTTP responses
	httpResponseChans := make([]chan string, 3) // One channel per command
	for i := range httpResponseChans {
		httpResponseChans[i] = make(chan string)
	}

	// Create functions to send each command.
	httpSenders := make([]func(), 3)
	for i, cmd := range []string{"S1Z1RT?", "S1MODE?", "S1Z1FAN!LOW"} {
		i := i
		cmd := cmd
		httpSenders[i] = func() {
			go func() {
				resp, err := http.Post(server.URL+"/command", "text/plain", strings.NewReader(cmd))
				if err != nil {
					log.Fatalf("Error sending command '%s': %v", cmd, err)
				}
				defer resp.Body.Close()
				body, _ := ioutil.ReadAll(resp.Body)
				httpResponseChans[i] <- string(body)
			}()
		}
	}

	// 1. Send the first read and wait for the device request.
	httpSenders[0]()
	cmd := <-fakeDevice.testCommandChan
	if cmd != "S1Z1RT?" {
		t.Errorf("Expected 'S1Z1RT?', got '%s'", cmd)
	}

	// 2. Send the second read and the write and wait for them to be pending.
	httpSenders[1]()
	httpSenders[2]()
	var pendingReads, pendingWrites int
	for { // Try until the expected queue state is reached
		handler.mu.Lock()
		pendingReads = len(handler.pendingReads)
		pendingWrites = len(handler.pendingWrites)
		handler.mu.Unlock()
		if pendingReads == 1 && pendingWrites == 1 {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}

	if pendingReads != 1 || pendingWrites != 1 {
		t.Errorf("Unexpected pending commands after retry: reads=%d, writes=%d", pendingReads, pendingWrites)
	}

	// 3. Send the response for the first read and verify the HTTP response
	fakeDevice.testResponseChan <- "S1Z1RT:72\xF8F"
	resp := <-httpResponseChans[0]
	if !strings.Contains(resp, `{"response": "72F"}`) {
		t.Errorf("Expected '{\"response\": \"72F\"}', got '%s'", resp)
	}

	// 4. Verify the write command is sent to the device next.
	cmd = <-fakeDevice.testCommandChan
	if cmd != "S1Z1FAN!LOW" {
		t.Errorf("Expected 'S1Z1FAN!LOW', got '%s'", cmd)
	}

	// 5. Send the response for the write and verify the HTTP response
	fakeDevice.testResponseChan <- "S1Z1FAN:ACK"
	resp = <-httpResponseChans[2]
	if !strings.Contains(resp, `{"response": "ACK"}`) {
		t.Errorf("Expected '{\"response\": \"ACK\"}', got '%s'", resp)
	}

	// 6. Verify the second read command is sent to the device next.
	cmd = <-fakeDevice.testCommandChan
	if cmd != "S1MODE?" {
		t.Errorf("Expected 'S1MODE?', got '%s'", cmd)
	}

	// 7. Send the response for the second read and verify the HTTP response
	fakeDevice.testResponseChan <- "S1MODE:HEAT"
	resp = <-httpResponseChans[1]
	if !strings.Contains(resp, `{"response": "HEAT"}`) {
		t.Errorf("Expected '{\"response\": \"HEAT\"}', got '%s'", resp)
	}
}

func TestCommands(t *testing.T) {
	// Test plan: send commands to the device over HTTP and verify that the
	// responses are correct.
	fakeDevice := newFakeDevice()
	fakeDevice.start()

	handler := new(commandHandler)
	handler.Open(fakeDevice)

	server := httptest.NewServer(handler)
	defer server.Close()

	// Test Cases
	testCases := []struct {
		name           string
		body           string
		expectedCode   int
		expectedBody   string
		deviceResponse string // Response for this test case
	}{
		{
			name:           "Read Current Temperature",
			body:           "S1Z1RT?",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "72F"}`,
			deviceResponse: "S1Z1RT:72\xF8F",
		},
		{
			name:           "Read Fan Mode",
			body:           "S1Z1FAN?",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "AUTO"}`,
			deviceResponse: "S1Z1FAN:AUTO",
		},
		{
			name:           "Read HVAC Mode",
			body:           "S1MODE?",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "HEAT"}`,
			deviceResponse: "S1MODE:HEAT",
		},
		{
			name:           "Read Target Temperature High",
			body:           "S1Z1CLSP?",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "75F"}`,
			deviceResponse: "S1Z1CLSP:75\xF8F",
		},
		{
			name:           "Read Target Temperature Low",
			body:           "S1Z1HTSP?",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "70F"}`,
			deviceResponse: "S1Z1HTSP:70\xF8F",
		},
		{
			name:           "Write Cool Setpoint",
			body:           "S1Z1CLSP!76",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "ACK"}`,
			deviceResponse: "S1Z1CLSP:ACK",
		},
		{
			name:           "Write HVAC Mode",
			body:           "S1MODE!COOL",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "ACK"}`,
			deviceResponse: "S1MODE:ACK",
		},
		{
			name:           "Write Fan Mode",
			body:           "S1Z1FAN!LOW",
			expectedCode:   http.StatusOK,
			expectedBody:   `{"response": "ACK"}`,
			deviceResponse: "S1Z1FAN:ACK",
		},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			// Create a request
			req, _ := http.NewRequest("POST", server.URL+"/command", strings.NewReader(tc.body))

			// Send the request and get the response
			respBody := make(chan string)
			respCode := make(chan int)
			go func() {
				r, err := server.Client().Do(req)
				if err != nil {
					log.Fatalf("Error sending request: %v", err)
				}
				defer r.Body.Close()
				body, err := ioutil.ReadAll(r.Body)
				if err != nil {
					log.Fatalf("Error reading body")
				}
				respCode <- r.StatusCode
				respBody <- string(body)
			}()

			// Wait for the command to be received by the fake device
			cmd := <-fakeDevice.testCommandChan

			// Verify the received command
			if cmd != tc.body {
				t.Errorf("Expected command '%s', got '%s'", tc.body, cmd)
			}

			// Send the correct response from the fake device
			fakeDevice.testResponseChan <- tc.deviceResponse

			// Check the status code
			if <-respCode != tc.expectedCode {
				t.Errorf("Expected status code %d, got %d", tc.expectedCode, respCode)
			}

			// Check the response body (if applicable)
			if tc.expectedBody != "" {
				body := <-respBody
				if !strings.Contains(body, tc.expectedBody) {
					t.Errorf("Expected body to contain '%s', got '%s'", tc.expectedBody, body)
				}
			}
		})
	}
}

func TestInvalidCommand(t *testing.T) {
	// Test plan: send an invalid command and verify that an HTTP error is
	// returned.
	fakeDevice := newFakeDevice()
	fakeDevice.start()

	handler := new(commandHandler)
	handler.Open(fakeDevice)

	server := httptest.NewServer(handler)
	defer server.Close()

	// This command will never succeed.
	cmd := "S1Z1FAN!BOGUS"
	go func() {
		for {
			if rc := <-fakeDevice.testCommandChan; rc != cmd {
				t.Errorf("Got %v, want %v", rc, cmd)
			}
			fakeDevice.testResponseChan <- "S1Z1FAN:NAK"
		}
	}()

	req, _ := http.NewRequest("POST", server.URL+"/command", strings.NewReader(cmd))
	resp, err := server.Client().Do(req)
	if err != nil {
		t.Fatalf("Error sending request: %v", err)
	}
	defer resp.Body.Close()
	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("Error reading body")
	}
	if resp.StatusCode != 500 {
		t.Errorf("Got %v, want %v", resp.StatusCode, 500)
	}
	if !strings.Contains(string(body), "Rejected command") {
		t.Errorf("Got %v, want a string containing 'Rejected command'", string(body))
	}
}

func TestConcurrency(t *testing.T) {
	// Test plan: send a concurrent mix of reads and writes using a real HTTP server.
	// Verify all requests return with code 200.
	fakeDevice := newFakeDevice()
	fakeDevice.start()

	handler := new(commandHandler)
	handler.Open(fakeDevice)

	m := http.NewServeMux()
	m.Handle("/command", handler)

	server := &http.Server{
		Addr:    ":8080",
		Handler: m,
	}
	server.SetKeepAlivesEnabled(true)
	go server.ListenAndServe()

	for i := 0; i < 6; i++ {
		go func() {
			for {
				client := &http.Client{
					Timeout: 30 * time.Second,
				}
				cmd := ""
				if rand.Int()%100 < 50 {
					cmd = "S1Z1MODE!HEAT"
				} else {
					cmd = "S1Z1MODE?"
				}
				req, _ := http.NewRequest("POST", "http://localhost:8080/command", strings.NewReader(cmd))
				resp, err := client.Do(req)
				if err != nil {
					log.Fatalf("Error sending request: %v", err)
				}
				resp.Body.Close()
				if resp.StatusCode != 200 {
					log.Fatalf("Got %v, want %v", resp.StatusCode, 200)
				}
			}
		}()
	}
	go func() {
		for {
			cmd := <-fakeDevice.testCommandChan
			if strings.Contains(cmd, "!") {
				fakeDevice.testResponseChan <- "S1Z1MODE:ACK"
			} else {
				fakeDevice.testResponseChan <- "S1Z1MODE:COOL"
			}
		}
	}()
	<-time.After(10 * time.Second)
}
