
import qrcode
import sys

print("===== SlotSync QR Code Generator =====")

# Ask for the SlotSync website URL
url = input("Enter your SlotSync URL: ").strip()

# Validate URL
if not url.startswith(("http://", "https://")):
    print("Invalid URL. Please enter a valid HTTP or HTTPS URL.")
    sys.exit(1)

# Generate QR code
qr = qrcode.QRCode(
    version=1,
    box_size=10,
    border=4
)

qr.add_data(url)
qr.make(fit=True)

# Create QR image
image = qr.make_image(
    fill_color="black",
    back_color="white"
)

# Save the QR code
image.save("slotsync_qr.png")

print("\nQR Code generated successfully!")
print("Website:", url)
print("Saved as: slotsync_qr.png")
