# Trust anchors for digital signatures

Indian digital signature certificates are issued by licensed certifying authorities under the
Controller of Certifying Authorities (CCA), whose root certificates anchor the India PKI under the
IT Act, 2000. These files let `circverify` check that a signed PDF's certificate chain leads to one of
those roots, offline and the same way on every machine (the operating system's trust store is not used).

All files were downloaded on 2026-11-13 over HTTPS from the CCA's own site, from the links on
<https://cca.gov.in/root_certificate.html> and <https://cca.gov.in/ca_certificates.html>, and
converted to PEM. The site publishes no fingerprints, so each file was compared with the copies
that signed UGC circulars embed in their signatures: where the column says "yes", the downloaded file
and the copy carried inside a PDF from ugc.gov.in are byte for byte the same certificate.

## Roots (trust anchors)

| File | Certificate | Valid | Matches a copy in a signed UGC PDF | SHA-256 |
|---|---|---|---|---|
| `roots/CCAIndia2014.pem` | CCA India 2014 | 2014-03-05 to 2024-03-05 | yes | `60109bc6c38328598a112c7a25e38b0f23e5a7511cb815fb64e0c4ff05db7df7` |
| `roots/CCAIndia2015SPL.pem` | CCA India 2015 SPL | 2015-01-29 to 2025-01-29 | no | `c34c5df53080078ffe45b21a7f600469917204f4f0293f1d7209393e5265c04f` |
| `roots/CCAIndia2022.pem` | CCA India 2022 | 2022-02-02 to 2042-02-02 | yes | `9a3fd3176798e842ddcb12c262f11cfacca70a8b84c6ea6fda30842a95a94cd8` |
| `roots/CCAIndia2022SPL.pem` | CCA India 2022 SPL | 2022-09-20 to 2042-09-20 | no | `b724689b79b2ef9421ef8f5cc733eb093851b170ee715177005a09f226d8c91a` |

The two SPL ("special purpose") roots come from the same CCA page but no indexed PDF embeds them, so
they could not be cross-checked.

## Intermediates (used to build chains, not trusted on their own)

Some signed PDFs carry only the signer's certificate. These licensed-CA certificates let the chain be
completed without fetching anything.

| File | Certificate | Valid | Matches a copy in a signed UGC PDF | SHA-256 |
|---|---|---|---|---|
| `intermediates/SafeScrypt-CA-2022.pem` | SafeScrypt CA 2022 | 2022-02-16 to 2032-02-16 | yes | `66d2c07740525f4524d1617758f9c316ad2b955eeec792ef493ce64c4ad99efa` |
| `intermediates/nCode-CA-2014.pem` | (n)Code Solutions CA 2014 | 2014-03-05 to 2024-03-05 | yes | `8198bbae38ace5e1038cc3b36b3e6be584c534a3da116401eaf28c17f73efdbf` |
| `intermediates/nCode-CA-2022.pem` | (n)Code Solutions CA 2022 | 2022-03-16 to 2032-03-16 | no | `89277300f4ca8511d6b4585d62c4ff6f765477169928c5bef49a88958a392595` |

## What a "trusted" result means

The chain is checked as of the signing time stated in the signature (certificates expire; a circular
signed in 2021 was signed with a certificate valid then), and revocation is not checked, since that
needs the network. Expired roots stay here for the same reason: they are what old signatures chain to.
To add other roots, list PEM or DER files in `CIRCVERIFY_TRUST_ROOTS` (separated like `PATH`).
