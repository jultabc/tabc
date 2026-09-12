package sh.tabc;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.MessageDigest;
import java.security.Signature;
import java.security.interfaces.EdECPrivateKey;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class TabcClientTest {

    @TempDir Path tempDir;

    private HttpServer server;
    private URI baseUri;
    private KeyPair nodeKey;
    private Path privateKeyFile;
    private final AtomicReference<String> requestBody = new AtomicReference<>();
    private final AtomicReference<String> requestNode = new AtomicReference<>();
    private final AtomicReference<String> requestTs = new AtomicReference<>();
    private final AtomicReference<String> requestSig = new AtomicReference<>();
    private final AtomicInteger responseStatus = new AtomicInteger(200);
    private final AtomicReference<String> responseBody =
            new AtomicReference<>("{\"id\":\"m-test\",\"info\":{\"recipients\":[\"hu\"]}}");

    @BeforeEach
    void setUp() throws Exception {
        nodeKey = KeyPairGenerator.getInstance("Ed25519").generateKeyPair();
        byte[] rawPrivate = ((EdECPrivateKey) nodeKey.getPrivate()).getBytes().orElseThrow();
        privateKeyFile = tempDir.resolve(".node_key.sample-program");
        Files.write(privateKeyFile, rawPrivate);
        Files.setPosixFilePermissions(privateKeyFile, PosixFilePermissions.fromString("rw-------"));

        server =
                HttpServer.create(
                        new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.createContext("/send", this::handleSend);
        server.start();
        baseUri = URI.create("http://127.0.0.1:" + server.getAddress().getPort());
    }

    @AfterEach
    void tearDown() {
        if (server != null) server.stop(0);
    }

    @Test
    void sendsSignedProgramEventAndPreservesConfiguredPrefix() throws Exception {
        TabcClient client = client();

        TabcClient.SendResult result =
                client.sendAsync(
                                List.of("hu"),
                                "[PROGRAM_EVENT] ENTRY_SIGNAL",
                                "{\"type\":\"ENTRY_SIGNAL\",\"ticker\":\"\ud14c\uc2a4\ud2b8\"}",
                                "now",
                                "event-123")
                        .get();

        assertEquals(TabcClient.Outcome.STORED, result.outcome());
        assertEquals(200, result.httpStatus());
        assertEquals("sample-program", requestNode.get());
        assertTrue(requestBody.get().contains("\"to\":[\"hu\"]"));
        assertTrue(requestBody.get().contains("\"subject\":\"[PROGRAM_EVENT] ENTRY_SIGNAL\""));
        assertTrue(requestBody.get().contains("\ud14c\uc2a4\ud2b8"));
        assertTrue(requestBody.get().contains("\"priority\":\"now\""));
        assertTrue(requestBody.get().contains("\"message_id\":\"event-123\""));

        byte[] canonical =
                canonicalRequest(
                        "sample-program", "POST", "/send", requestBody.get(), requestTs.get());
        Signature verifier = Signature.getInstance("Ed25519");
        verifier.initVerify(nodeKey.getPublic());
        verifier.update(canonical);
        assertTrue(verifier.verify(base58Decode(requestSig.get())));
    }

    @Test
    void mapsServerRefusalToFailedWithoutClaimingStorage() {
        responseStatus.set(400);
        responseBody.set("{\"error\":\"program nodes are send-only\"}");

        TabcClient.SendResult result =
                client().send(List.of("other-program"), "event", "body", "now");

        assertEquals(TabcClient.Outcome.FAILED, result.outcome());
        assertEquals(400, result.httpStatus());
        assertTrue(result.responseBody().contains("send-only"));
    }

    @Test
    void mapsConnectionFailureToUnknown() {
        server.stop(0);
        server = null;

        TabcClient.SendResult result = client().send(List.of("hu"), "event", "body", "now");

        assertEquals(TabcClient.Outcome.UNKNOWN, result.outcome());
        assertEquals(-1, result.httpStatus());
        assertTrue(!result.responseBody().isBlank());
    }

    @Test
    void refusesNodeKeyReadableByGroupOrOthers() throws Exception {
        Files.setPosixFilePermissions(privateKeyFile, PosixFilePermissions.fromString("rw-r--r--"));

        org.junit.jupiter.api.Assertions.assertThrows(
                IllegalArgumentException.class, this::client);
    }

    private TabcClient client() {
        return new TabcClient(
                baseUri,
                "sample-program",
                privateKeyFile,
                Duration.ofSeconds(1),
                Duration.ofSeconds(2));
    }

    private void handleSend(HttpExchange exchange) throws java.io.IOException {
        requestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        requestNode.set(exchange.getRequestHeaders().getFirst("X-Node"));
        requestTs.set(exchange.getRequestHeaders().getFirst("X-Node-Ts"));
        requestSig.set(exchange.getRequestHeaders().getFirst("X-Node-Sig"));
        byte[] body = responseBody.get().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.sendResponseHeaders(responseStatus.get(), body.length);
        exchange.getResponseBody().write(body);
        exchange.close();
    }

    private static byte[] canonicalRequest(
            String node, String method, String path, String body, String ts) throws Exception {
        String canonical =
                String.join(
                        "\n", sha256(node), sha256(method), sha256(path), sha256(body), sha256(ts));
        return canonical.getBytes(StandardCharsets.UTF_8);
    }

    private static String sha256(String value) throws Exception {
        byte[] digest =
                MessageDigest.getInstance("SHA-256")
                        .digest(value.getBytes(StandardCharsets.UTF_8));
        return java.util.HexFormat.of().formatHex(digest);
    }

    private static byte[] base58Decode(String encoded) {
        String alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
        java.math.BigInteger value = java.math.BigInteger.ZERO;
        for (int i = 0; i < encoded.length(); i++) {
            int digit = alphabet.indexOf(encoded.charAt(i));
            if (digit < 0) throw new IllegalArgumentException("invalid base58");
            value = value.multiply(java.math.BigInteger.valueOf(58)).add(java.math.BigInteger.valueOf(digit));
        }
        byte[] signed = value.toByteArray();
        int offset = signed.length > 1 && signed[0] == 0 ? 1 : 0;
        int leadingZeros = 0;
        while (leadingZeros < encoded.length() && encoded.charAt(leadingZeros) == '1') {
            leadingZeros++;
        }
        byte[] out = new byte[leadingZeros + signed.length - offset];
        System.arraycopy(signed, offset, out, leadingZeros, signed.length - offset);
        return out;
    }
}
