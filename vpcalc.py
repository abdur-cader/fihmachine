def calculate_vp(itemprice, current):
    available_bundles = {475:5, 1000:10, 2050:20, 3650:35, 5350:50, 11000:100}
    sorted_bundles = sorted(available_bundles.items(), reverse=True)
    
    remaining = itemprice - current #remaining amount of vp needed
    
    if remaining <= 0:
        #print("ya already have enough")
        return "", 0
    
    total = 0
    cart = {}
    
    for vp, cost in sorted_bundles: 
        if remaining <= 0:
            break
        count = remaining // vp
        if count > 0:
            cart[vp] = count 
            total += count * cost #add to total amount the current total sum of amount of bundles
            remaining -= count * vp
    
    if remaining > 0:
        b475 = sorted_bundles[-1]
        cart[b475[0]] = cart.get(b475[0], 0) + 1
        total += b475[1]
        

    details = [f"{count}x {vp} VP" for vp, count in cart.items()] 
    details_str = "\n".join(details)  # Join details for embed
    return details_str, total  # Return details and total amount
