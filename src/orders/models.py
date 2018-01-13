import math

from django.conf import settings
from django.db import models
from django.db.models.signals import pre_save, post_save
from django.core.urlresolvers import reverse

from kart.utils import unique_order_id_generator
from carts.models import Cart
from billing.models import BillingProfile
from addresses.models import Address
from products.models import Product


ORDER_STATUS_CHOICES = (
    # (Database value, Display Value) # This separation is automatically done by the choices field in model
    ('created', 'Created'),
    ('paid', 'Paid'),
    ('shipped', 'Shipped'),
    ('refunded', 'Refunded'),
    ('cancelled', 'Cancelled'),
    ('delivered', 'Delivered')
)


class OrderQuerySet(models.query.QuerySet):

    def by_request(self, request):
        billing_profile, created = BillingProfile.objects.get_or_new(request)
        return self.filter(billing_profile=billing_profile)

    def not_created(self):
        return self.exclude(status='created')


class OrderManager(models.Manager):

    def get_queryset(self):
        return OrderQuerySet(self.model, using=self._db)

    def by_request(self, request):
        return self.get_queryset().by_request(request)

    def get_or_new(self, billing_profile, cart_obj):
        qs = self.get_queryset().filter(
            billing_profile=billing_profile, cart=cart_obj, active=True, status='created'
        )
        created = False
        if qs.count() == 1:
            obj = qs.first()
        else:
            obj = self.model.objects.create(billing_profile=billing_profile, cart=cart_obj)
            created = True
        return obj, created


class Order(models.Model):
    order_id = models.CharField(max_length=120, blank=True)
    billing_profile = models.ForeignKey(BillingProfile, null=True, blank=True)
    shipping_address = models.ForeignKey(Address, related_name='shipping_address', null=True, blank=True)
    billing_address = models.ForeignKey(Address, related_name='billing_address', null=True, blank=True)
    cart = models.ForeignKey(Cart)
    status = models.CharField(max_length=120, default='created', choices=ORDER_STATUS_CHOICES)
    shipping_total = models.DecimalField(default=50.00, max_digits=100, decimal_places=2)
    total = models.DecimalField(default=0.00, max_digits=100, decimal_places=2)
    active = models.BooleanField(default=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = OrderManager()

    class Meta:
        ordering = ['-timestamp', '-updated']

    def __str__(self):
        return self.order_id

    def get_absolute_url(self):
        return reverse('orders:detail', kwargs={'order_id': self.order_id})

    def get_status(self):
        if self.status == 'shipped':
            return 'Shipped'
        elif self.status == 'refunded':
            return 'Refunded'
        elif self.status == 'cancelled':
            return 'Cancelled'
        elif self.status == 'delivered':
            return 'Delivered'
        return 'Shipping Soon'

    def update_total(self):
        new_total = math.fsum([self.cart.total, self.shipping_total])
        self.total = format(new_total, '.2f')
        self.save()
        return self.total

    def check_done(self):
        shipping_address_required = not self.cart.is_digital
        shipping_done = False
        if shipping_address_required:
            if self.shipping_address:
                shipping_done = True
            else:
                shipping_done = False
        else:
            shipping_done = True
        if self.billing_profile and shipping_done and self.billing_address and self.total > 0:
            return True
        return False

    def mark_paid(self):
        if self.status != 'paid':
            if self.check_done():
                self.status = 'paid'
                self.save()
                # iterate through purchased products
        return self.status


# Generate order_id
def order_id_pre_save_receiver(sender, instance, *args, **kwargs):
    if not instance.order_id:
        instance.order_id = unique_order_id_generator(instance)
    # Since billing profile is associated to order during checkout, initially due to consecutive
    # login and logouts with guest or user, there can be multiple orders associated with a cart
    # so we set all those orders which are not associated to the billing profile to be inactive
    qs = Order.objects.filter(cart=instance.cart).exclude(billing_profile=instance.billing_profile)
    if qs.exists():
        qs.update(active=False)

pre_save.connect(order_id_pre_save_receiver, sender=Order)


# It is used to update the order total when the cart is updated
# This method is executed only when changes occur in cart
# Here, instance = Cart
def cart_total_post_save_receiver(sender, instance, created, *args, **kwargs):
    if not created:  # 'created' checks if the cart has just been created
        # If the cart was just created then we don't execute the following code because
        # newly created cart won't have any order
        qs = Order.objects.filter(cart__id=instance.id)  # Orders associated with the cart
        if qs.count() == 1:
            order_obj = qs.first()
            order_obj.update_total()

post_save.connect(cart_total_post_save_receiver, sender=Cart)


# This method is executed when the order object is modified, whether it is directly or through the cart
def order_post_save_receiver(sender, instance, created, *args, **kwargs):
    # The check below ensures that the code here is executed only once when the order is created
    # For rest of the times, the order total should change only with cart updation
    # If the check is not present, this method is executed repeatedly and thus giving RecursionDepthError
    if created:  # If the order has just been created, then enter the block
        instance.update_total()

post_save.connect(order_post_save_receiver, sender=Order)


class ProductPurchaseManager(models.Manager):

    def all(self):
        return self.get_queryset().filter(refunded=False)


class ProductPurchase(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, blank=True, null=True)
    billing_profile = models.ForeignKey(BillingProfile)
    product = models.ForeignKey(Product)
    refunded = models.BooleanField(default=False)
    updated = models.DateTimeField(auto_now=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    objects = ProductPurchaseManager()

    def __str__(self):
        return self.product.title
