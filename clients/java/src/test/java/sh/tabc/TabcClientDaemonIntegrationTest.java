package sh.tabc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Tag;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

@Tag("integration")
class TabcClientDaemonIntegrationTest {

    private static final String RECIPIENT = "n-java-recipient";
    private static final String SENDER = "n-java-program";

    @TempDir Path tempDir;

    private Path repositoryRoot;
    private Path stateHome;
    private Path daemonLog;
    private String python;
    private URI baseUri;
    private Process daemon;

    @BeforeEach
    void startIsolatedDaemon() throws Exception {
        repositoryRoot =
                Path.of(System.getProperty("tabc.repo.root"))
                        .toAbsolutePath()
                        .normalize();
        stateHome = Files.createDirectories(tempDir.resolve("home"));
        daemonLog = tempDir.resolve("tabd.log");
        python = System.getenv().getOrDefault("TABC_TEST_PYTHON", "python3");

        startDaemonWithPortRetry();

        assertCliSuccess(
                "register recipient",
                "-m",
                "tabus.cli",
                "register",
                "--node",
                RECIPIENT,
                "--kind",
                "codex");
        assertCliSuccess(
                "register program sender",
                "-m",
                "tabus.cli",
                "register",
                "--node",
                SENDER,
                "--kind",
                "engine",
                "--program");
    }

    @AfterEach
    void stopIsolatedDaemon() throws Exception {
        stopDaemon();
    }

    private void startDaemonWithPortRetry() throws Exception {
        AssertionError lastFailure = null;
        for (int attempt = 1; attempt <= 3; attempt++) {
            int port = availablePort();
            baseUri = URI.create("http://127.0.0.1:" + port);
            daemonLog = tempDir.resolve("tabd-attempt-" + attempt + ".log");
            ProcessBuilder builder =
                    pythonProcess(
                            "-m",
                            "tabus.daemon",
                            "--bind",
                            "127.0.0.1",
                            "--port",
                            Integer.toString(port));
            builder.redirectErrorStream(true);
            builder.redirectOutput(daemonLog.toFile());
            daemon = builder.start();
            try {
                awaitDaemon(port);
                return;
            } catch (AssertionError startFailure) {
                lastFailure = startFailure;
                stopDaemon();
            }
        }
        throw new AssertionError("tabd failed to start after 3 isolated ports", lastFailure);
    }

    private void stopDaemon() throws Exception {
        if (daemon == null) return;
        daemon.destroy();
        if (!daemon.waitFor(2, TimeUnit.SECONDS)) {
            daemon.destroyForcibly();
            daemon.waitFor(2, TimeUnit.SECONDS);
        }
        daemon = null;
    }

    @Test
    void pythonDaemonAcceptsRealJavaSignatureAndRejectsWrongKey() throws Exception {
        TabcClient client = client(stateHome.resolve(".node_key." + SENDER));

        TabcClient.SendResult stored =
                client.send(
                        List.of(RECIPIENT),
                        "[PROGRAM_EVENT] E2E_TEST",
                        "{\"type\":\"E2E_TEST\",\"source\":\"java-client\"}",
                        "now",
                        "n-java-e2e-stored");

        assertEquals(TabcClient.Outcome.STORED, stored.outcome());
        assertEquals(200, stored.httpStatus());
        String mailboxBeforeForgery = cli("-m", "tabus.cli", "dm", "--node", RECIPIENT);
        assertTrue(mailboxBeforeForgery.contains("1 unread"));
        assertTrue(mailboxBeforeForgery.contains("[PROGRAM_EVENT] E2E_TEST"));

        TabcClient forged = client(stateHome.resolve(".node_key." + RECIPIENT));
        TabcClient.SendResult rejected =
                forged.send(
                        List.of(RECIPIENT),
                        "[PROGRAM_EVENT] FORGED",
                        "wrong-key",
                        "now",
                        "n-java-e2e-forged");

        assertEquals(TabcClient.Outcome.FAILED, rejected.outcome());
        assertEquals(401, rejected.httpStatus());
        String mailboxAfterForgery = cli("-m", "tabus.cli", "dm", "--node", RECIPIENT);
        assertTrue(mailboxAfterForgery.contains("1 unread"));
        assertFalse(mailboxAfterForgery.contains("[PROGRAM_EVENT] FORGED"));
    }

    @Test
    void pythonDaemonRefusesCodedBodyAndRequestSizeAsFailed() throws Exception {
        TabcClient client = client(stateHome.resolve(".node_key." + SENDER));

        TabcClient.SendResult body =
                client.send(List.of(RECIPIENT), "[PROGRAM_EVENT] BODY_65537", "a".repeat(65537), "now", "n-java-body");
        assertEquals(TabcClient.Outcome.FAILED, body.outcome());
        assertEquals(400, body.httpStatus());
        assertTrue(body.responseBody().contains("\"code\": \"BODY_TOO_LARGE\""), body.responseBody());

        // Over the 524,288-byte request limit and under the discard limit: the daemon reads the
        // body before answering, so the refusal reaches the client every time.
        for (int i = 0; i < 5; i++) {
            TabcClient.SendResult request =
                    client.send(
                            List.of(RECIPIENT),
                            "[PROGRAM_EVENT] REQUEST_600000",
                            "a".repeat(600000),
                            "now",
                            "n-java-request-" + i);
            assertEquals(TabcClient.Outcome.FAILED, request.outcome(), request.responseBody());
            assertEquals(413, request.httpStatus());
            assertTrue(request.responseBody().contains("\"code\": \"REQUEST_TOO_LARGE\""), request.responseBody());
        }

        String mailbox = cli("-m", "tabus.cli", "dm", "--node", RECIPIENT);
        assertFalse(mailbox.contains("BODY_65537"), mailbox);
        assertFalse(mailbox.contains("REQUEST_600000"), mailbox);
    }

    @Test
    void requestOverTheDiscardLimitIsNeverReportedStored() throws Exception {
        TabcClient client = client(stateHome.resolve(".node_key." + SENDER));

        // 17 MiB: the daemon refuses without reading. The client may see the 413 or only a closed
        // connection; either way nothing is stored and the result is not STORED.
        TabcClient.SendResult result =
                client.send(
                        List.of(RECIPIENT),
                        "[PROGRAM_EVENT] REQUEST_17MIB",
                        "a".repeat(17 * 1024 * 1024),
                        "now",
                        "n-java-request-17mib");
        System.out.println("17 MiB request: " + result.outcome() + " " + result.httpStatus() + " " + result.responseBody());

        if (result.outcome() == TabcClient.Outcome.FAILED) {
            assertEquals(413, result.httpStatus());
        } else {
            assertEquals(TabcClient.Outcome.UNKNOWN, result.outcome());
        }
        String mailbox = cli("-m", "tabus.cli", "dm", "--node", RECIPIENT);
        assertFalse(mailbox.contains("REQUEST_17MIB"), mailbox);
    }

    private TabcClient client(Path keyPath) {
        return new TabcClient(
                baseUri, SENDER, keyPath, Duration.ofSeconds(1), Duration.ofSeconds(3));
    }

    private void awaitDaemon(int port) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(8);
        IOException lastFailure = null;
        while (System.nanoTime() < deadline) {
            if (!daemon.isAlive()) {
                throw new AssertionError(
                        "tabd exited before accepting requests:\n" + daemonOutput());
            }
            try (Socket ignored = new Socket("127.0.0.1", port)) {
                return;
            } catch (IOException notReady) {
                lastFailure = notReady;
                Thread.sleep(50);
            }
        }
        throw new AssertionError(
                "tabd did not listen within 8 seconds: " + lastFailure + "\n" + daemonOutput());
    }

    private void assertCliSuccess(String action, String... arguments) throws Exception {
        CliResult result = runCli(arguments);
        assertEquals(
                0,
                result.exitCode(),
                action + " failed:\n" + result.output() + "\n" + daemonOutput());
    }

    private String cli(String... arguments) throws Exception {
        CliResult result = runCli(arguments);
        assertEquals(
                0,
                result.exitCode(),
                "tabc command failed:\n" + result.output() + "\n" + daemonOutput());
        return result.output();
    }

    private CliResult runCli(String... arguments) throws Exception {
        Process process = pythonProcess(arguments).redirectErrorStream(true).start();
        if (!process.waitFor(10, TimeUnit.SECONDS)) {
            process.destroyForcibly();
            process.waitFor(2, TimeUnit.SECONDS);
            throw new AssertionError(
                    "tabc command did not finish within 10 seconds\n" + daemonOutput());
        }
        String output = new String(process.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
        return new CliResult(process.exitValue(), output);
    }

    private ProcessBuilder pythonProcess(String... arguments) {
        ProcessBuilder builder = new ProcessBuilder();
        builder.command().add(python);
        builder.command().addAll(List.of(arguments));
        builder.directory(repositoryRoot.toFile());
        builder.environment().put("TABC_DB", tempDir.resolve("tabc.db").toString());
        builder.environment().put("TABC_HOME", stateHome.toString());
        builder.environment().put("TABC_BUS_URL", baseUri.toString());
        builder.environment().put("TABC_DOORBELL_SPOOL", tempDir.resolve("spool").toString());
        String existingPythonPath = builder.environment().getOrDefault("PYTHONPATH", "");
        String separator = System.getProperty("path.separator");
        builder.environment()
                .put(
                        "PYTHONPATH",
                        repositoryRoot
                                + (existingPythonPath.isBlank()
                                        ? ""
                                        : separator + existingPythonPath));
        return builder;
    }

    private String daemonOutput() throws IOException {
        return Files.exists(daemonLog) ? Files.readString(daemonLog) : "(no tabd log)";
    }

    private static int availablePort() throws IOException {
        try (ServerSocket socket = new ServerSocket(0)) {
            return socket.getLocalPort();
        }
    }

    private record CliResult(int exitCode, String output) {}
}
