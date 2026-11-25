// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {ChronosReaper} from "../src/ChronosReaper.sol";
import {IFlashLoanSimpleReceiver, IPool, IPoolAddressesProvider} from "../src/interfaces/IAaveV3.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";
import {ISwapRouter} from "../src/interfaces/ISwapRouter.sol";

contract MockERC20 is IERC20 {
    string public name;
    mapping(address => uint256) public override balanceOf;
    mapping(address => mapping(address => uint256)) public override allowance;

    constructor(string memory _name) {
        name = _name;
    }

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 value) external override returns (bool) {
        allowance[msg.sender][spender] = value;
        return true;
    }

    function transfer(address to, uint256 value) external override returns (bool) {
        require(balanceOf[msg.sender] >= value, "insufficient");
        balanceOf[msg.sender] -= value;
        balanceOf[to] += value;
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external override returns (bool) {
        require(balanceOf[from] >= value, "insufficient");
        require(allowance[from][msg.sender] >= value, "allowance");
        allowance[from][msg.sender] -= value;
        balanceOf[from] -= value;
        balanceOf[to] += value;
        return true;
    }
}

contract MockSwapRouter is ISwapRouter {
    // Fixed numerator/denominator rate in bps (10_000 = 1:1)
    uint256 public rateBps;

    constructor(uint256 _rateBps) {
        rateBps = _rateBps;
    }

    function setRateBps(uint256 _rateBps) external {
        rateBps = _rateBps;
    }

    function exactInputSingle(ExactInputSingleParams calldata params) external payable override returns (uint256 amountOut) {
        IERC20(params.tokenIn).transferFrom(msg.sender, address(this), params.amountIn);
        amountOut = (params.amountIn * rateBps) / 10_000;
        require(amountOut >= params.amountOutMinimum, "slippage");
        MockERC20(params.tokenOut).mint(params.recipient, amountOut);
    }
}

contract MockAavePool is IPool {
    uint256 public constant PREMIUM_BPS = 5; // 0.05%

    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16
    ) external override {
        uint256 premium = (amount * PREMIUM_BPS) / 10_000;
        IERC20(asset).transfer(receiverAddress, amount);

        bool ok = IFlashLoanSimpleReceiver(receiverAddress).executeOperation(
            asset, amount, premium, msg.sender, params
        );
        require(ok, "callback failed");

        IERC20(asset).transferFrom(receiverAddress, address(this), amount + premium);
    }
}

contract MockAddressesProvider is IPoolAddressesProvider {
    address public immutable pool;

    constructor(address _pool) {
        pool = _pool;
    }

    function getPool() external view override returns (address) {
        return pool;
    }
}

contract ChronosReaperTest is Test {
    MockERC20 internal usdc;
    MockERC20 internal wbtc;
    MockSwapRouter internal routerBuy;
    MockSwapRouter internal routerSell;
    MockAavePool internal pool;
    MockAddressesProvider internal provider;
    ChronosReaper internal reaper;

    address internal owner = address(this);
    address internal attacker = address(0xBEEF);

    receive() external payable {}

    function setUp() public {
        usdc = new MockERC20("USDC");
        wbtc = new MockERC20("WBTC");
        routerBuy = new MockSwapRouter(10_000);  // 1:1 leg 1
        routerSell = new MockSwapRouter(10_100); // +1.00% spread on leg 2
        pool = new MockAavePool();
        provider = new MockAddressesProvider(address(pool));
        reaper = new ChronosReaper(address(provider));

        usdc.mint(address(pool), 10_000_000e6);
    }

    function _defaultParams(uint256 minProfit) internal view returns (ChronosReaper.ArbitrageParams memory) {
        return ChronosReaper.ArbitrageParams({
            routerBuy: address(routerBuy),
            routerSell: address(routerSell),
            intermediateToken: address(wbtc),
            feeBuy: 500,
            feeSell: 3000,
            minIntermediateOut: 0,
            minProfit: minProfit
        });
    }

    function test_InitiateStrike_ProfitableArbitrage() public {
        uint256 loanAmount = 1_000_000e6;
        uint256 expectedGross = 1_010_000e6;
        uint256 expectedFee = 500e6; // 0.05%
        uint256 expectedNet = expectedGross - loanAmount - expectedFee; // 9,500e6

        uint256 ownerBefore = usdc.balanceOf(owner);
        reaper.initiateStrike(address(usdc), loanAmount, _defaultParams(1_000e6));
        uint256 ownerAfter = usdc.balanceOf(owner);

        assertEq(ownerAfter - ownerBefore, expectedNet);
        assertEq(usdc.balanceOf(address(reaper)), 0);
        assertEq(usdc.balanceOf(address(pool)), 10_000_000e6 + expectedFee);
    }

    function test_RevertWhen_NonOwnerInitiatesStrike() public {
        vm.prank(attacker);
        vm.expectRevert(ChronosReaper.Unauthorized.selector);
        reaper.initiateStrike(address(usdc), 1_000e6, _defaultParams(0));
    }

    function test_RevertWhen_DirectCallbackCall() public {
        vm.prank(attacker);
        vm.expectRevert(ChronosReaper.InvalidPoolCallback.selector);
        reaper.executeOperation(address(usdc), 1_000e6, 5e5, address(reaper), abi.encode(_defaultParams(0)));
    }

    function test_RevertWhen_InvalidInitiator() public {
        vm.prank(address(pool));
        vm.expectRevert(ChronosReaper.InvalidInitiator.selector);
        reaper.executeOperation(address(usdc), 1_000e6, 5e5, attacker, abi.encode(_defaultParams(0)));
    }

    function test_RevertWhen_UnprofitableSpread() public {
        routerSell.setRateBps(9_990); // -0.10% loss
        vm.expectRevert("slippage");
        reaper.initiateStrike(address(usdc), 1_000_000e6, _defaultParams(100e6));
    }

    function test_RevertWhen_Paused() public {
        reaper.setPaused(true);
        vm.expectRevert(ChronosReaper.ContractPaused.selector);
        reaper.initiateStrike(address(usdc), 1_000e6, _defaultParams(0));
    }

    function test_TwoStepOwnershipTransfer() public {
        reaper.transferOwnership(attacker);
        assertEq(reaper.pendingOwner(), attacker);
        assertEq(reaper.owner(), owner);

        vm.prank(attacker);
        reaper.acceptOwnership();
        assertEq(reaper.owner(), attacker);
        assertEq(reaper.pendingOwner(), address(0));
    }

    function test_EmergencyWithdrawAndETH() public {
        usdc.mint(address(reaper), 5_000e6);
        vm.deal(address(reaper), 1 ether);

        uint256 beforeUsdc = usdc.balanceOf(owner);
        uint256 beforeEth = owner.balance;

        reaper.emergencyWithdraw(address(usdc));
        reaper.emergencyWithdrawETH();

        assertEq(usdc.balanceOf(owner) - beforeUsdc, 5_000e6);
        assertEq(owner.balance - beforeEth, 1 ether);
    }
}
