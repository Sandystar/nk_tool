// nplicense — local NeoKikoeru licence responder for natively patched builds.
//
// Protocol (v3.32.x):
//
//	POST /license/{activate,verify,deactivate}
//	request:  {"params":"<b64>","enc_key":"<b64>","sign":"<hex>","ts":<unix>}
//	reply 200: gob(ActivationWithToken{Token: <PKCS#8 PEM bytes>, Signature: ...})
//
// The patched client has NP3 (the keystream XOR over Token) turned into an
// identity and NP6 (signature pre-check) turned into "return nil", so the PEM
// can be handed over verbatim.  The embedded RSA public key is replaced by the
// matching SPKI (NP4/NP5), which makes the licence key usable by the client's
// own envelope crypto afterwards.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"embed"
	"encoding/gob"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"
)

//go:embed key_pkcs8.pem
var embeddedFS embed.FS

// ActivationWithToken mirrors the fields the client's gob decoder looks for.
// It deliberately carries both the old v3.28 field name (Secret) and the new
// v3.32 field name (Token); gob ignores fields the peer does not know, so one
// binary serves both protocol generations.
type ActivationWithToken struct {
	AppName    string
	Version    string
	DeviceID   string
	OS         string
	LicenseKey string
	Secret     []byte
	Token      []byte
	Signature  []byte
	Timestamp  int64
}

type requestBody struct {
	Params string `json:"params"`
	EncKey string `json:"enc_key"`
	Sign   string `json:"sign"`
	TS     int64  `json:"ts"`
}

var (
	privKey  *rsa.PrivateKey
	secretPE []byte
	logFile  *os.File
	logger   = log.New(os.Stdout, "", log.LstdFlags)
)

func osName() string {
	if v := os.Getenv("NK_LIC_OS"); v != "" {
		return v
	}
	return runtime.GOOS
}

func loadKey(path string) error {
	var raw []byte
	var err error
	if path != "" {
		raw, err = os.ReadFile(path)
		if err != nil {
			return fmt.Errorf("read key %s: %w", path, err)
		}
	} else if exe, e := os.Executable(); e == nil {
		side := filepath.Join(filepath.Dir(exe), "private_key.pem")
		if b, e2 := os.ReadFile(side); e2 == nil {
			raw, path = b, side
		}
	}
	if raw == nil {
		raw, err = embeddedFS.ReadFile("key_pkcs8.pem")
		path = "<embedded>"
		if err != nil {
			return fmt.Errorf("no key: %w", err)
		}
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return fmt.Errorf("%s: not a PEM file", path)
	}
	var key any
	switch block.Type {
	case "PRIVATE KEY":
		key, err = x509.ParsePKCS8PrivateKey(block.Bytes)
	case "RSA PRIVATE KEY":
		key, err = x509.ParsePKCS1PrivateKey(block.Bytes)
	default:
		if key, err = x509.ParsePKCS8PrivateKey(block.Bytes); err != nil {
			key, err = x509.ParsePKCS1PrivateKey(block.Bytes)
		}
	}
	if err != nil {
		return fmt.Errorf("%s: parse: %w", path, err)
	}
	rk, ok := key.(*rsa.PrivateKey)
	if !ok {
		return fmt.Errorf("%s: not an RSA key (%T)", path, key)
	}
	// The client requires PEM type "PRIVATE KEY" (11 bytes) + PKCS#8.
	der, err := x509.MarshalPKCS8PrivateKey(rk)
	if err != nil {
		return err
	}
	privKey = rk
	secretPE = pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	return nil
}

func encode(a ActivationWithToken) []byte {
	var buf bytes.Buffer
	if err := gob.NewEncoder(&buf).Encode(a); err != nil {
		logger.Printf("gob encode failed: %v", err)
		return nil
	}
	return buf.Bytes()
}

func handler(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	var rb requestBody
	_ = json.Unmarshal(body, &rb)

	short := strings.TrimPrefix(r.URL.Path, "/license/")
	msg := fmt.Sprintf("%-6s /license/%-34s from=%-15s len=%-5d ts=%-11d",
		r.Method, short, r.RemoteAddr, len(body), rb.TS)
	logger.Print(msg)
	if logFile != nil {
		fmt.Fprintln(logFile, msg)
	}

	if r.URL.Path == "/health" {
		fmt.Fprintln(w, "ok")
		return
	}

	// deactivate: the client only needs success; an empty body is enough.
	if strings.HasPrefix(r.URL.Path, "/license/deactivate") {
		w.WriteHeader(200)
		return
	}

	payload := encode(ActivationWithToken{
		AppName:    "neokikoeru",
		Version:    os.Getenv("NK_LIC_VERSION"),
		DeviceID:   os.Getenv("NK_LIC_DEVICE"),
		OS:         osName(),
		LicenseKey: os.Getenv("NK_LIC_KEY"),
		Secret:     secretPE,
		Token:      secretPE,
		Signature:  []byte("nktool"),
		Timestamp:  time.Now().Unix(),
	})
	if payload == nil {
		http.Error(w, "encode error", 500)
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", fmt.Sprint(len(payload)))
	w.WriteHeader(200)
	_, _ = w.Write(payload)
}

func main() {
	addr := flag.String("addr", "127.0.0.1:18443", "listen address (must match the URL patched into the binary)")
	keyPath := flag.String("key", "", "private key PEM (default: private_key.pem next to this exe, else embedded)")
	logPath := flag.String("log", "", "also append the request log to this file")
	quiet := flag.Bool("quiet", false, "no per-request log lines")
	watchStdin := flag.Bool("watch-stdin", false, "exit when stdin reaches EOF (used by the start script)")
	flag.Parse()

	if *quiet {
		logger.SetOutput(io.Discard)
	}
	if err := loadKey(*keyPath); err != nil {
		fmt.Fprintf(os.Stderr, "[!] %v\n", err)
		os.Exit(1)
	}
	if *logPath != "" {
		f, err := os.OpenFile(*logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err == nil {
			logFile = f
			defer f.Close()
		}
	}
	sum := sha256.Sum256(secretPE)
	logger.Printf("nplicense listening on http://%s  (RSA-%d, PKCS#8 PEM %d bytes, sha256 %x)",
		*addr, privKey.N.BitLen(), len(secretPE), sum[:8])

	if *watchStdin {
		go func() {
			_, _ = io.Copy(io.Discard, os.Stdin)
			logger.Print("stdin closed, exiting")
			os.Exit(0)
		}()
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           http.HandlerFunc(handler),
		ReadHeaderTimeout: 10 * time.Second,
	}
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintf(os.Stderr, "[!] %v\n", err)
		os.Exit(1)
	}
}

var _ = rand.Reader
